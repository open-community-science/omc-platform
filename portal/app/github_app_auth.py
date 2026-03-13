"""GitHub App authentication — JWT + installation token flow.

A GitHub App authenticates in two steps:
1. Sign a JWT with the app's private key (valid 10 min)
2. Exchange the JWT for an installation access token (valid 1 hour)

The installation token is what we use for API calls (repo creation, PRs, etc.).
Falls back to PAT auth if GitHub App isn't configured.
"""
import time
import logging
import jwt  # PyJWT
import httpx

from .config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

# Cache the installation token (valid ~1 hour)
_cached_token: str | None = None
_cached_token_expires: float = 0


def _is_app_configured() -> bool:
    """Check if GitHub App credentials are available."""
    return bool(settings.github_app_id and settings.github_app_private_key)


def _build_jwt() -> str:
    """Build a signed JWT for GitHub App authentication.

    The JWT is signed with RS256 using the app's private key.
    Valid for 10 minutes (GitHub's max).
    """
    now = int(time.time())
    payload = {
        "iat": now - 60,           # issued at (60s clock skew buffer)
        "exp": now + (10 * 60),    # expires in 10 minutes
        "iss": settings.github_app_id,
    }

    # Private key can be a file path or the PEM content directly
    private_key = settings.github_app_private_key
    if private_key.startswith("/") or private_key.startswith("~"):
        from pathlib import Path
        private_key = Path(private_key).expanduser().read_text()

    return jwt.encode(payload, private_key, algorithm="RS256")


async def _get_installation_id(jwt_token: str) -> int:
    """Find the installation ID for our org."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{GITHUB_API}/app/installations",
            headers={
                "Authorization": f"Bearer {jwt_token}",
                "Accept": "application/vnd.github.v3+json",
            },
        )
        resp.raise_for_status()

        installations = resp.json()
        for inst in installations:
            account = inst.get("account", {})
            if account.get("login") == settings.github_org:
                return inst["id"]

        # If only one installation, use it
        if len(installations) == 1:
            return installations[0]["id"]

        raise RuntimeError(
            f"No GitHub App installation found for org '{settings.github_org}'. "
            f"Install the app at https://github.com/organizations/{settings.github_org}/settings/installations"
        )


async def _create_installation_token(jwt_token: str, installation_id: int) -> tuple[str, float]:
    """Exchange JWT for an installation access token."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {jwt_token}",
                "Accept": "application/vnd.github.v3+json",
            },
        )
        resp.raise_for_status()

        data = resp.json()
        token = data["token"]
        # Parse expiry — GitHub returns ISO 8601
        expires_at = data.get("expires_at", "")
        # Cache for 50 minutes (token lasts 60)
        expires_ts = time.time() + 50 * 60
        return token, expires_ts


async def get_github_token() -> str:
    """Get a valid GitHub token for API calls.

    Prefers GitHub App (installation token) over PAT.
    Caches the installation token for ~50 minutes.
    """
    global _cached_token, _cached_token_expires

    # If App isn't configured, fall back to PAT
    if not _is_app_configured():
        if settings.github_token:
            return settings.github_token
        raise RuntimeError(
            "No GitHub credentials configured. "
            "Set GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY, or GITHUB_TOKEN."
        )

    # Return cached token if still valid
    if _cached_token and time.time() < _cached_token_expires:
        return _cached_token

    # Generate new installation token
    try:
        jwt_token = _build_jwt()
        installation_id = await _get_installation_id(jwt_token)
        token, expires_ts = await _create_installation_token(jwt_token, installation_id)

        _cached_token = token
        _cached_token_expires = expires_ts
        logger.info("Refreshed GitHub App installation token")
        return token

    except Exception as e:
        logger.error(f"GitHub App auth failed: {e}")
        # Fall back to PAT if available
        if settings.github_token:
            logger.warning("Falling back to PAT auth")
            return settings.github_token
        raise


async def get_github_headers() -> dict:
    """Get headers for GitHub API calls (works with App or PAT)."""
    token = await get_github_token()
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
