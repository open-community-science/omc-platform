"""OpenRouter OAuth PKCE integration — lets users bring their own LLM credits."""

import base64
import hashlib
import logging
import secrets

import httpx
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import attributes
from pathlib import Path

from .auth import require_user
from .config import get_settings
from .crypto import encrypt_value, decrypt_value
from .database import get_db, User

logger = logging.getLogger(__name__)
router = APIRouter(tags=["openrouter"])
settings = get_settings()

BASE_DIR = Path(__file__).parent.parent
from .templating import templates  # shared instance (globals + filters)

OPENROUTER_AUTH_URL = "https://openrouter.ai/auth"
OPENROUTER_KEY_EXCHANGE_URL = "https://openrouter.ai/api/v1/auth/keys"

# Default model when using OpenRouter (user can change later)
OPENROUTER_DEFAULT_MODEL = "anthropic/claude-sonnet-4"


def _make_callback_url(request: Request) -> str:
    """Build the OAuth callback URL from the current request."""
    # In production behind nginx, use X-Forwarded headers
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.url.netloc)
    return f"{scheme}://{host}/settings/openrouter/callback"


def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


# ── Settings page ─────────────────────────────────────────────────────────────

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """User settings page."""
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "user": user,
            "openrouter_connected": bool(user.openrouter_key),
            "openrouter_model": user.openrouter_model or OPENROUTER_DEFAULT_MODEL,
        },
    )


# ── OAuth PKCE flow ──────────────────────────────────────────────────────────

@router.get("/settings/openrouter/connect")
async def openrouter_connect(
    request: Request,
    user: User = Depends(require_user),
):
    """Redirect user to OpenRouter to authorize and create an API key."""
    verifier, challenge = _generate_pkce()

    # Store verifier in session cookie for the callback
    request.session["openrouter_code_verifier"] = verifier

    callback_url = _make_callback_url(request)
    auth_url = (
        f"{OPENROUTER_AUTH_URL}"
        f"?callback_url={callback_url}"
        f"&code_challenge={challenge}"
        f"&code_challenge_method=S256"
    )
    return RedirectResponse(auth_url)


@router.get("/settings/openrouter/callback")
async def openrouter_callback(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Exchange authorization code for an OpenRouter API key."""
    code = request.query_params.get("code")
    verifier = request.session.pop("openrouter_code_verifier", None)

    if not code or not verifier:
        logger.warning("OpenRouter callback missing code or verifier")
        return RedirectResponse("/settings?error=missing_code", status_code=303)

    # Exchange code for API key
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                OPENROUTER_KEY_EXCHANGE_URL,
                json={
                    "code": code,
                    "code_verifier": verifier,
                    "code_challenge_method": "S256",
                },
            )

        if resp.status_code != 200:
            logger.warning(f"OpenRouter key exchange failed: {resp.status_code} {resp.text[:200]}")
            return RedirectResponse("/settings?error=exchange_failed", status_code=303)

        api_key = resp.json().get("key")
        if not api_key:
            return RedirectResponse("/settings?error=no_key", status_code=303)

    except Exception as e:
        logger.error(f"OpenRouter key exchange error: {e}")
        return RedirectResponse("/settings?error=exchange_error", status_code=303)

    # Encrypt and store
    user.openrouter_key = encrypt_value(api_key)
    attributes.flag_modified(user, "openrouter_key")
    await db.commit()

    # Invalidate LLM proxy cache
    from .llm_proxy import _openrouter_cache
    _openrouter_cache.pop(user.id, None)

    logger.info(f"User {user.github_login} connected OpenRouter")
    return RedirectResponse("/settings?connected=1", status_code=303)


@router.post("/settings/openrouter/disconnect")
async def openrouter_disconnect(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Remove stored OpenRouter API key."""
    user.openrouter_key = None
    attributes.flag_modified(user, "openrouter_key")
    await db.commit()

    # Invalidate LLM proxy cache
    from .llm_proxy import _openrouter_cache
    _openrouter_cache.pop(user.id, None)

    logger.info(f"User {user.github_login} disconnected OpenRouter")
    return RedirectResponse("/settings?disconnected=1", status_code=303)


@router.post("/settings/openrouter/model")
async def openrouter_set_model(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Update the user's preferred OpenRouter model."""
    form = await request.form()
    model = str(form.get("model", "")).strip()
    if not model:
        return RedirectResponse("/settings?error=no_model", status_code=303)

    # Test the model with a tiny request before saving
    if not user.openrouter_key:
        return RedirectResponse("/settings?error=not_connected", status_code=303)

    api_key = decrypt_value(user.openrouter_key)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "HTTP-Referer": "https://microbial.opencommunity.science",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "Say OK"}],
                    "max_tokens": 5,
                },
            )
        if resp.status_code == 429:
            logger.warning(f"Model {model} rate-limited: {resp.text[:200]}")
            return RedirectResponse(f"/settings?error=rate_limited&model={model}", status_code=303)
        if resp.status_code != 200:
            logger.warning(f"Model {model} test failed: {resp.status_code} {resp.text[:200]}")
            return RedirectResponse(f"/settings?error=model_failed&model={model}", status_code=303)
    except Exception as e:
        logger.warning(f"Model {model} test error: {e}")
        return RedirectResponse(f"/settings?error=model_failed&model={model}", status_code=303)

    user.openrouter_model = model
    attributes.flag_modified(user, "openrouter_model")
    await db.commit()

    # Invalidate LLM proxy cache
    from .llm_proxy import _openrouter_cache
    _openrouter_cache.pop(user.id, None)

    logger.info(f"User {user.github_login} set OpenRouter model to {model}")
    return RedirectResponse("/settings?model_saved=1", status_code=303)


@router.get("/settings/openrouter/models")
async def openrouter_list_models(
    request: Request,
    user: User = Depends(require_user),
):
    """Fetch available models from OpenRouter, filtered by relevant categories."""
    if not user.openrouter_key:
        raise HTTPException(400, "OpenRouter not connected")

    api_key = decrypt_value(user.openrouter_key)
    headers = {"Authorization": f"Bearer {api_key}"}

    # Fetch models from relevant categories, then filter for tool support
    CATEGORIES = ["science", "academia", "programming"]

    try:
        seen_ids = set()
        all_models = []
        async with httpx.AsyncClient(timeout=15.0) as client:
            for category in CATEGORIES:
                resp = await client.get(
                    "https://openrouter.ai/api/v1/models",
                    headers=headers,
                    params={"category": category},
                )
                if resp.status_code == 200:
                    for m in resp.json().get("data", []):
                        mid = m.get("id", "")
                        if mid not in seen_ids:
                            seen_ids.add(mid)
                            all_models.append(m)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"OpenRouter API error: {e}")

    models = []
    for m in all_models:
        model_id = m.get("id", "")
        name = m.get("name", model_id)
        ctx = m.get("context_length", 0)
        pricing = m.get("pricing", {})
        prompt_price = pricing.get("prompt", "0")
        completion_price = pricing.get("completion", "0")

        if ctx < 8000:
            continue

        # Require tool/function calling support
        supported = m.get("supported_parameters", [])
        if not isinstance(supported, list) or "tools" not in supported:
            continue

        is_free = (str(prompt_price) in ("0", "0.0", "0.00")
                   and str(completion_price) in ("0", "0.0", "0.00"))

        models.append({
            "id": model_id,
            "name": name,
            "context_length": ctx,
            "free": is_free,
            "prompt_price": prompt_price,
            "completion_price": completion_price,
        })

    # Sort: free first, then by name
    models.sort(key=lambda m: (not m["free"], m["name"].lower()))

    return {"models": models}
