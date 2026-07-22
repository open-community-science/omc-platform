"""LLM backend selection: local, shared (admin) OpenRouter, or personal OpenRouter.

Users pick a backend explicitly (see /settings); this module resolves that choice
into concrete {backend, base_url, api_key, model, label} for every AI feature, so
the model that produced a piece of output can always be named.

Resolution order when the user hasn't chosen (or their choice is unavailable):
personal key → shared admin key → local LLM. Every AI call should go through
resolve_llm() so the picker and the provenance label stay consistent.
"""
import logging

import httpx
from sqlalchemy import select

from .config import get_settings
from .crypto import decrypt_value
from .database import (
    async_session, SiteConfig, User,
    SITE_OPENROUTER_KEY, SITE_OPENROUTER_MODEL,
)

logger = logging.getLogger(__name__)
settings = get_settings()

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

BACKEND_LOCAL = "local"
BACKEND_ADMIN = "admin"
BACKEND_PERSONAL = "personal"

# Free-tier OpenRouter models offered for the shared/admin backend. Recommended
# first — it is the default when a user picks a backend without naming a model.
FREE_OPENROUTER_MODELS = [
    "deepseek/deepseek-r1:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "google/gemma-3-27b-it:free",
    "qwen/qwen3-235b-a22b:free",
]
# Sensible default when the user connects a personal key (they have credits).
PERSONAL_DEFAULT_MODEL = "anthropic/claude-sonnet-4"
# Preferred local model, if the server actually serves it (see recommended_local_model).
LOCAL_PREFERRED = ["openai/gpt-oss-120b", "google/gemma-3-27b", "openai/gpt-oss-20b"]


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
    model = await get_site_config(SITE_OPENROUTER_MODEL)
    return {"key": key, "model": model or FREE_OPENROUTER_MODELS[0]}


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


def recommended_local_model(available: list[str]) -> str:
    """Pick the best local model we know about, else the first served one."""
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
        model = chosen_model if chosen_model.endswith(":free") else cfg["model"]
        return {
            "backend": BACKEND_ADMIN, "base_url": OPENROUTER_BASE_URL,
            "api_key": cfg["key"], "model": model,
            "label": f"OpenRouter · {model} (shared key)",
        }

    async def _local() -> dict:
        model = chosen_model
        if not model or "/" not in model or model.endswith(":free"):
            model = recommended_local_model(await list_local_models())
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
