"""LLM backend selection: local, shared (admin) OpenRouter, or personal OpenRouter.

Users pick a backend explicitly (see /settings); this module resolves that choice
into concrete {backend, base_url, api_key, model, label} for every AI feature, so
the model that produced a piece of output can always be named.

Resolution order when the user hasn't chosen (or their choice is unavailable):
personal key → shared admin key → local LLM. Every AI call should go through
resolve_llm() so the picker and the provenance label stay consistent.
"""
import logging
import time

import httpx
from sqlalchemy import select

from .config import get_settings
from .crypto import decrypt_value
from .database import (
    async_session, SiteConfig, User,
    SITE_OPENROUTER_KEY, SITE_OPENROUTER_MODEL, SITE_LOCAL_MODEL,
)

logger = logging.getLogger(__name__)
settings = get_settings()

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

BACKEND_LOCAL = "local"
BACKEND_ADMIN = "admin"
BACKEND_PERSONAL = "personal"

# Sensible default when the user connects a personal key (they have credits).
# OpenRouter's `~`-prefixed floating alias always points at the newest Sonnet.
PERSONAL_DEFAULT_MODEL = "~anthropic/claude-sonnet-latest"
# Preferred local model, if the server actually serves it (see recommended_local_model).
LOCAL_PREFERRED = ["openai/gpt-oss-120b", "google/gemma-3-27b", "openai/gpt-oss-20b"]

# OpenRouter categories worth listing (tool-capable science/coding models).
_OR_CATEGORIES = ["science", "academia", "programming"]
# Cache the fetched OpenRouter catalogue briefly so resolve_llm() (called per
# request) never hits the network on the hot path, and the model browser stays
# snappy. Keyed by free_only. No hardcoded model list anywhere — the free
# default is always whatever OpenRouter actually offers right now.
_or_cache: dict[bool, tuple[float, list[dict]]] = {}
_OR_CACHE_TTL = 600  # seconds


async def fetch_openrouter_models(api_key: str, free_only: bool = False) -> list[dict]:
    """Fetch tool-capable OpenRouter models in the uniform table shape (cached).

    Shared by the free (admin key) and personal sources so both stay live and
    consistent. Only the free-tier list is cached across users (it's the same
    for everyone); the personal catalogue is per-key and not cached here.
    """
    if free_only:
        hit = _or_cache.get(True)
        if hit and (time.monotonic() - hit[0]) < _OR_CACHE_TTL:
            return hit[1]

    headers = {"Authorization": f"Bearer {api_key}"}
    seen: set[str] = set()
    raw: list[dict] = []
    async with httpx.AsyncClient(timeout=15.0) as client:
        for category in _OR_CATEGORIES:
            resp = await client.get(
                f"{OPENROUTER_BASE_URL}/models", headers=headers,
                params={"category": category},
            )
            if resp.status_code == 200:
                for m in resp.json().get("data", []):
                    mid = m.get("id", "")
                    if mid and mid not in seen:
                        seen.add(mid)
                        raw.append(m)

    models = []
    for m in raw:
        ctx = m.get("context_length", 0)
        if ctx < 8000:
            continue
        supported = m.get("supported_parameters", [])
        if not isinstance(supported, list) or "tools" not in supported:
            continue
        pricing = m.get("pricing", {})
        pp, cp = pricing.get("prompt", "0"), pricing.get("completion", "0")
        is_free = (str(pp) in ("0", "0.0", "0.00") and str(cp) in ("0", "0.0", "0.00"))
        if free_only and not is_free:
            continue
        models.append({
            "id": m.get("id", ""), "name": m.get("name", m.get("id", "")),
            "context_length": ctx, "free": is_free,
            "prompt_price": pp, "completion_price": cp,
        })
    models.sort(key=lambda m: (not m["free"], m["name"].lower()))

    if free_only and models:
        _or_cache[True] = (time.monotonic(), models)
    return models


async def free_default_model(api_key: str) -> str | None:
    """The model to use for the shared/free tier when the user names none.

    Always the first live free model OpenRouter offers — never a hardcoded id
    that can go stale. Returns None if the fetch fails or nothing is free.
    """
    try:
        models = await fetch_openrouter_models(api_key, free_only=True)
    except httpx.HTTPError as e:
        logger.warning("Could not fetch free OpenRouter models: %s", e)
        return None
    return models[0]["id"] if models else None


async def get_site_config(key: str) -> str | None:
    """Read a single site_config value."""
    async with async_session() as db:
        row = (await db.execute(select(SiteConfig).where(SiteConfig.key == key))).scalar_one_or_none()
        return row.value if row else None


async def set_site_config(key: str, value: str | None) -> None:
    """Upsert (or clear) a site_config value."""
    async with async_session() as db:
        row = (await db.execute(select(SiteConfig).where(SiteConfig.key == key))).scalar_one_or_none()
        if value is None:
            if row:
                await db.delete(row)
        elif row:
            row.value = value
        else:
            db.add(SiteConfig(key=key, value=value))
        await db.commit()


async def get_admin_openrouter() -> dict | None:
    """The shared OpenRouter credentials, if an admin has connected them."""
    enc = await get_site_config(SITE_OPENROUTER_KEY)
    if not enc:
        return None
    try:
        key = decrypt_value(enc)
    except Exception as e:
        logger.warning("Could not decrypt the admin OpenRouter key: %s", e)
        return None
    if not key:
        return None
    # The admin's explicitly-set default, if any. May be None — callers resolve
    # a live model via free_default_model() rather than a hardcoded fallback.
    model = await get_site_config(SITE_OPENROUTER_MODEL)
    return {"key": key, "model": model or None}


async def list_local_models() -> list[str]:
    """Model ids served by the local LLM right now (empty if it's unreachable)."""
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(f"{settings.llm_base_url.rstrip('/')}/models")
        if r.status_code != 200:
            return []
        return sorted(m["id"] for m in r.json().get("data", []) if m.get("id"))
    except Exception as e:
        logger.debug("Local LLM model list unavailable: %s", e)
        return []


async def recommended_local_model(available: list[str]) -> str:
    """The recommended local model: the admin's pick if it's actually served,
    then the best model we know about, then the first served one."""
    admin = await get_site_config(SITE_LOCAL_MODEL)
    if admin and admin in available:
        return admin
    for m in LOCAL_PREFERRED:
        if m in available:
            return m
    return available[0] if available else settings.llm_model


async def resolve_llm(user: User | None) -> dict:
    """Resolve a user's backend choice into a usable LLM config.

    Returns {backend, base_url, api_key, model, label}. `label` is the
    human-readable provenance string to show alongside any generated output.
    """
    choice = getattr(user, "llm_backend", None) if user else None
    chosen_model = (getattr(user, "llm_model", None) or "").strip() if user else ""

    async def _personal() -> dict | None:
        if not user or not user.openrouter_key:
            return None
        try:
            key = decrypt_value(user.openrouter_key)
        except Exception:
            return None
        if not key:
            return None
        model = chosen_model or user.openrouter_model or PERSONAL_DEFAULT_MODEL
        return {
            "backend": BACKEND_PERSONAL, "base_url": OPENROUTER_BASE_URL,
            "api_key": key, "model": model,
            "label": f"OpenRouter · {model} (your key)",
        }

    async def _admin() -> dict | None:
        cfg = await get_admin_openrouter()
        if not cfg:
            return None
        # Prefer the user's chosen free model, then the admin's set default,
        # then whatever OpenRouter currently offers free. Never a stale id.
        if chosen_model.endswith(":free"):
            model = chosen_model
        else:
            model = cfg["model"] or await free_default_model(cfg["key"])
        if not model:
            return None  # couldn't resolve a live free model → fall through
        return {
            "backend": BACKEND_ADMIN, "base_url": OPENROUTER_BASE_URL,
            "api_key": cfg["key"], "model": model,
            "label": f"OpenRouter · {model} (shared key)",
        }

    async def _local() -> dict:
        # Honour any model the local server actually serves — provider validity is
        # NOT inferable from a "/" in the id (e.g. 'codeqwen3-14b', 'gpt-oss-20b'
        # are valid local ids). Fall back only when empty, a hosted ':free' id, or
        # the saved id is no longer served (issue #32).
        model = chosen_model
        available = await list_local_models()
        if not model or model.endswith(":free") or (available and model not in available):
            model = await recommended_local_model(available)
        return {
            "backend": BACKEND_LOCAL, "base_url": settings.llm_base_url,
            "api_key": settings.llm_api_key, "model": model,
            "label": f"Local LLM · {model}",
        }

    # Honour an explicit choice, but never dead-end if it isn't usable.
    if choice == BACKEND_PERSONAL:
        return await _personal() or await _admin() or await _local()
    if choice == BACKEND_ADMIN:
        return await _admin() or await _local()
    if choice == BACKEND_LOCAL:
        return await _local()
    # No explicit choice — best available.
    return await _personal() or await _admin() or await _local()
