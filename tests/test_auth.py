"""Dev mode authentication."""
import httpx
import pytest

BASE = "http://127.0.0.1:8002"


@pytest.mark.asyncio
async def test_dev_login_redirects_to_dashboard():
    async with httpx.AsyncClient(base_url=BASE, follow_redirects=True) as c:
        r = await c.get("/auth/login")
        assert r.status_code == 200
        assert "/dashboard" in str(r.url)


@pytest.mark.asyncio
async def test_dashboard_shows_login_when_unauthenticated():
    async with httpx.AsyncClient(base_url=BASE) as c:
        r = await c.get("/dashboard")
        assert r.status_code == 200
        # Should show login prompt, not user content
        assert "Sign in" in r.text or "login" in r.text.lower()
