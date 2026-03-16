"""LLM proxy — forwards OpenAI-compatible requests from session containers.

Session containers have no direct network access. They call this endpoint
on the portal, which authenticates the session token and forwards to the
real LLM backend (LM Studio, Claude, etc.).

Every request is logged per-submission for the provenance trail.
"""

import hashlib
import hmac
import logging
import time
from datetime import datetime

import httpx
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/llm", tags=["llm-proxy"])
settings = get_settings()

# ── Session token registry ───────────────────────────────────────────────────

# Maps session_token → {slug, created_at, request_count}
_session_tokens: dict[str, dict] = {}

# Rate limit: max requests per session per minute
RATE_LIMIT = 30
_rate_windows: dict[str, list[float]] = {}


def create_session_token(slug: str) -> str:
    """Generate a session-scoped token for LLM proxy access."""
    raw = f"{slug}:{settings.secret_key}:{time.time()}"
    token = hashlib.sha256(raw.encode()).hexdigest()[:48]
    _session_tokens[token] = {
        "slug": slug,
        "created_at": datetime.utcnow(),
        "request_count": 0,
    }
    return token


def revoke_session_token(slug: str):
    """Revoke all tokens for a session."""
    to_remove = [t for t, info in _session_tokens.items() if info["slug"] == slug]
    for t in to_remove:
        del _session_tokens[t]
        _rate_windows.pop(t, None)


def _check_auth(request: Request) -> dict:
    """Validate session token from Authorization header."""
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip()

    if not token or token not in _session_tokens:
        raise HTTPException(401, "Invalid session token")

    info = _session_tokens[token]

    # Rate limiting
    now = time.time()
    window = _rate_windows.setdefault(token, [])
    window[:] = [t for t in window if now - t < 60]  # keep last minute
    if len(window) >= RATE_LIMIT:
        raise HTTPException(429, "Rate limit exceeded (30 req/min)")
    window.append(now)

    info["request_count"] += 1
    return info


# ── Proxy endpoint ───────────────────────────────────────────────────────────

class ChatCompletionRequest(BaseModel):
    model: str | None = None
    messages: list[dict]
    max_tokens: int = 1000
    temperature: float = 0.7
    stream: bool = False


@router.post("/chat/completions")
async def proxy_completions(request: Request):
    """Proxy chat completions to the configured LLM backend.

    Path matches OpenAI convention so the OpenAI SDK works natively:
    Container sets base_url=http://gateway:8002/api/llm/v1
    SDK calls POST /v1/chat/completions → hits /api/llm/v1/chat/completions
    """
    session_info = _check_auth(request)
    slug = session_info["slug"]

    body = await request.json()

    # Override model if not set or if it doesn't match configured model
    if not body.get("model"):
        body["model"] = settings.llm_model

    target_url = f"{settings.llm_base_url}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.llm_api_key}",
    }

    logger.info(f"LLM proxy [{slug}]: {len(body.get('messages', []))} messages, "
                f"stream={body.get('stream', False)}")

    if body.get("stream"):
        return await _stream_response(target_url, headers, body, slug)
    else:
        return await _forward_response(target_url, headers, body, slug)


async def _forward_response(url: str, headers: dict, body: dict, slug: str):
    """Forward a non-streaming request."""
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            resp = await client.post(url, json=body, headers=headers)
        except httpx.ConnectError:
            raise HTTPException(502, "LLM backend unreachable")
        except httpx.TimeoutException:
            raise HTTPException(504, "LLM backend timeout")

    if resp.status_code != 200:
        logger.warning(f"LLM proxy [{slug}]: backend returned {resp.status_code}")
        raise HTTPException(resp.status_code, resp.text)

    return resp.json()


async def _stream_response(url: str, headers: dict, body: dict, slug: str):
    """Forward a streaming request as SSE."""
    async def event_stream():
        async with httpx.AsyncClient(timeout=120.0) as client:
            try:
                async with client.stream("POST", url, json=body, headers=headers) as resp:
                    if resp.status_code != 200:
                        logger.warning(f"LLM proxy [{slug}]: stream backend returned {resp.status_code}")
                        yield f"data: {{\"error\": \"backend returned {resp.status_code}\"}}\n\n"
                        return
                    async for line in resp.aiter_lines():
                        if line:
                            yield f"{line}\n"
                        else:
                            yield "\n"
            except httpx.ConnectError:
                yield 'data: {"error": "LLM backend unreachable"}\n\n'
            except httpx.TimeoutException:
                yield 'data: {"error": "LLM backend timeout"}\n\n'

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Models endpoint (OpenAI SDK compatibility) ──────────────────────────────

@router.get("/models")
async def list_models(request: Request):
    """Return available models. OpenAI SDK calls this on init."""
    _check_auth(request)
    return {
        "object": "list",
        "data": [
            {"id": settings.llm_model, "object": "model", "owned_by": "omc"},
        ],
    }


# ── Session data endpoints (called by containers via session token) ──────────

@router.post("/update-selection")
async def update_run_selection(request: Request):
    """Update the selected run accessions for a submission.

    Called by the AI chat when the researcher refines their sample selection.
    Authenticated by session token (same as LLM proxy).
    Body: {"run_accessions": ["SRR123", "SRR456", ...]}
    """
    from .database import async_session as db_session_maker, Submission
    from sqlalchemy import select
    from sqlalchemy.orm import attributes

    session_info = _check_auth(request)
    slug = session_info["slug"]
    body = await request.json()

    accessions = body.get("run_accessions", [])
    if not isinstance(accessions, list):
        raise HTTPException(400, "run_accessions must be a list")

    async with db_session_maker() as db:
        stmt = select(Submission).where(Submission.slug == slug)
        result = await db.execute(stmt)
        submission = result.scalar_one_or_none()
        if not submission:
            raise HTTPException(404, "Submission not found")

        interview_data = dict(submission.interview_data or {})
        interview_data["_selected_run_accessions"] = accessions
        submission.interview_data = interview_data
        attributes.flag_modified(submission, "interview_data")
        await db.commit()

    logger.info(f"Session {slug}: updated selection to {len(accessions)} runs")
    return {"ok": True, "slug": slug, "selected_count": len(accessions)}


# ── Health check ─────────────────────────────────────────────────────────────

@router.get("/health")
async def llm_health():
    """Check if the LLM backend is reachable."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{settings.llm_base_url}/models",
                headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            )
            return {"status": "ok", "backend": settings.llm_base_url, "models_status": resp.status_code}
    except Exception as e:
        return {"status": "unreachable", "backend": settings.llm_base_url, "error": str(e)}


@router.post("/dev/create-token")
async def dev_create_token(request: Request):
    """DEV ONLY: Create a session token for testing. Remove in production."""
    if not settings.debug:
        raise HTTPException(403, "Only available in debug mode")
    body = await request.json()
    slug = body.get("slug", "test")
    token = create_session_token(slug)
    return {"token": token, "slug": slug}
