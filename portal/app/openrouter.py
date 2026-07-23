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

# Recommended default for a user's own OpenRouter key (they can change later).
# The `~`-prefixed alias is OpenRouter's floating pointer to the newest Sonnet;
# it's a valid model id but doesn't appear in the category-filtered browse list,
# so the personal source injects it as a row (see list_models_for_source).
OPENROUTER_DEFAULT_MODEL = "~anthropic/claude-sonnet-latest"


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
    """User settings page — LLM backend choice + OpenRouter connection."""
    from .llm_backends import (
        list_local_models, recommended_local_model, get_admin_openrouter,
        resolve_llm,
    )
    local_models = await list_local_models()
    admin_cfg = await get_admin_openrouter()
    active = await resolve_llm(user)
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "user": user,
            "openrouter_connected": bool(user.openrouter_key),
            "openrouter_model": user.openrouter_model or OPENROUTER_DEFAULT_MODEL,
            # Backend picker. The model lists themselves are fetched live per
            # source by /settings/models — only availability + current choice
            # are rendered server-side here.
            "llm_backend": user.llm_backend or active["backend"],
            "llm_model": user.llm_model or "",
            "local_available": bool(local_models),
            "local_recommended": await recommended_local_model(local_models),
            "admin_key_available": bool(admin_cfg),
            "personal_recommended": OPENROUTER_DEFAULT_MODEL,
            "active_label": active["label"],
        },
    )


@router.post("/settings/llm")
async def set_llm_backend(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Save the user's LLM backend + model choice."""
    from .llm_backends import BACKEND_LOCAL, BACKEND_ADMIN, BACKEND_PERSONAL
    form = await request.form()
    backend = (form.get("backend") or "").strip()
    if backend not in (BACKEND_LOCAL, BACKEND_ADMIN, BACKEND_PERSONAL):
        raise HTTPException(status_code=400, detail="Unknown backend")
    # Each backend posts its own model field, so switching doesn't clobber the
    # model you'd picked for the others.
    model = (form.get(f"model_{backend}") or "").strip()
    user.llm_backend = backend
    user.llm_model = model or None
    if backend == BACKEND_PERSONAL and model:
        user.openrouter_model = model
    await db.commit()

    from .llm_proxy import _openrouter_cache
    _openrouter_cache.pop(user.id, None)
    return RedirectResponse("/settings?saved=1", status_code=303)


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


@router.get("/settings/models")
async def list_models_for_source(
    source: str,
    request: Request,
    user: User = Depends(require_user),
):
    """Unified model list for the settings picker, keyed by source.

    One endpoint feeds the single model browser: `local` lists the LM Studio
    server's models, `free` lists the shared-key OpenRouter free tier, and
    `personal` lists the user's own OpenRouter catalogue. Every source returns
    the same row shape so the client renders them all in one table. The
    `recommended` id is always drawn from the live list — never a hardcoded
    model that can go stale.
    """
    from .llm_backends import (
        list_local_models, recommended_local_model, get_admin_openrouter,
        fetch_openrouter_models,
    )

    if source == "local":
        ids = await list_local_models()
        rows = [{
            "id": m, "name": m, "context_length": 0,
            "free": True, "prompt_price": "0", "completion_price": "0",
            "local": True,
        } for m in ids]
        return {"models": rows, "available": bool(ids),
                "recommended": await recommended_local_model(ids) if ids else ""}

    if source == "free":
        cfg = await get_admin_openrouter()
        if not cfg:
            return {"models": [], "available": False}
        try:
            models = await fetch_openrouter_models(cfg["key"], free_only=True)
        except httpx.HTTPError as e:
            return {"models": [], "available": True, "error": str(e)}
        # Recommend the admin's set default only if it's actually still live;
        # otherwise the first live free model. Never a dead id.
        live_ids = {m["id"] for m in models}
        rec = cfg["model"] if cfg["model"] in live_ids else (models[0]["id"] if models else "")
        return {"models": models, "available": True, "recommended": rec}

    if source == "personal":
        if not user.openrouter_key:
            return {"models": [], "connected": False}
        try:
            models = await fetch_openrouter_models(decrypt_value(user.openrouter_key))
        except httpx.HTTPError as e:
            return {"models": [], "connected": True, "error": str(e)}
        live_ids = {m["id"] for m in models}
        # Recommend the user's own saved model if it's still live, otherwise our
        # default (the newest-Sonnet floating alias). Never a stale saved id.
        rec = user.openrouter_model if user.openrouter_model in live_ids else OPENROUTER_DEFAULT_MODEL
        # The default alias isn't in the category-filtered list, so surface it as
        # a row (unknown price/context) so it shows the chip and is selectable.
        if rec and rec not in live_ids:
            models.insert(0, {"id": rec, "name": rec, "context_length": 0,
                              "free": False, "prompt_price": None, "completion_price": None})
        return {"models": models, "connected": True, "recommended": rec}

    raise HTTPException(400, "Unknown source")


# ── Admin: site-wide (shared) OpenRouter key ─────────────────────────────────
# Same PKCE dance as the per-user flow, but the resulting key is stored in
# site_config so every user can select the "shared" backend without bringing
# their own credentials. Admin-gated; see llm_backends.resolve_llm.

def _make_admin_callback_url(request: Request) -> str:
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.url.netloc)
    return f"{scheme}://{host}/admin/openrouter/callback"


@router.get("/admin/openrouter/connect")
async def admin_openrouter_connect(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Start the OAuth flow for the shared/admin OpenRouter key."""
    from .auth import is_admin
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Admin access required")

    verifier, challenge = _generate_pkce()
    request.session["openrouter_admin_code_verifier"] = verifier
    callback_url = _make_admin_callback_url(request)
    return RedirectResponse(
        f"{OPENROUTER_AUTH_URL}"
        f"?callback_url={callback_url}"
        f"&code_challenge={challenge}"
        f"&code_challenge_method=S256"
    )


@router.get("/admin/openrouter/callback")
async def admin_openrouter_callback(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Exchange the code and store the key site-wide."""
    from .auth import is_admin
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Admin access required")

    code = request.query_params.get("code")
    verifier = request.session.pop("openrouter_admin_code_verifier", None)
    if not code or not verifier:
        return RedirectResponse("/admin?error=missing_code", status_code=303)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                OPENROUTER_KEY_EXCHANGE_URL,
                json={"code": code, "code_verifier": verifier,
                      "code_challenge_method": "S256"},
            )
        if resp.status_code != 200:
            logger.warning("Admin OpenRouter exchange failed: %s %s",
                           resp.status_code, resp.text[:200])
            return RedirectResponse("/admin?error=exchange_failed", status_code=303)
        api_key = resp.json().get("key")
        if not api_key:
            return RedirectResponse("/admin?error=no_key", status_code=303)
    except Exception as e:
        logger.error("Admin OpenRouter exchange error: %s", e)
        return RedirectResponse("/admin?error=exchange_error", status_code=303)

    from .database import SITE_OPENROUTER_KEY
    from .llm_backends import set_site_config
    await set_site_config(SITE_OPENROUTER_KEY, encrypt_value(api_key))
    logger.info("Admin %s connected the shared OpenRouter key", user.github_login)
    return RedirectResponse("/admin?connected=1", status_code=303)


@router.post("/admin/openrouter/disconnect")
async def admin_openrouter_disconnect(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Clear the shared OpenRouter key."""
    from .auth import is_admin
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Admin access required")
    from .database import SITE_OPENROUTER_KEY
    from .llm_backends import set_site_config
    await set_site_config(SITE_OPENROUTER_KEY, None)
    logger.info("Admin %s disconnected the shared OpenRouter key", user.github_login)
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/openrouter/model")
async def admin_openrouter_model(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Set the recommended model for the shared/free tier."""
    from .auth import is_admin
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Admin access required")
    form = await request.form()
    model = (form.get("model") or "").strip()
    from .database import SITE_OPENROUTER_MODEL
    from .llm_backends import set_site_config
    await set_site_config(SITE_OPENROUTER_MODEL, model or None)
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/local/model")
async def admin_local_model(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Set the recommended local model shown to users in Settings."""
    from .auth import is_admin
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Admin access required")
    form = await request.form()
    model = (form.get("model") or "").strip()
    from .database import SITE_LOCAL_MODEL
    from .llm_backends import set_site_config
    await set_site_config(SITE_LOCAL_MODEL, model or None)
    return RedirectResponse("/admin", status_code=303)
