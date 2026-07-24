"""Server health checks."""
import os
import httpx
import pytest

BASE = os.environ.get("OMC_TEST_BASE_URL", "http://127.0.0.1:8002")


# Drives a live portal over HTTP: skipped unless a dev instance is actually
# there (see tests/conftest.py).
pytestmark = pytest.mark.live_server

@pytest.mark.asyncio
async def test_landing_page():
    async with httpx.AsyncClient(base_url=BASE) as c:
        r = await c.get("/")
        assert r.status_code == 200
        assert "Open Microbial Community" in r.text


@pytest.mark.asyncio
async def test_static_css():
    async with httpx.AsyncClient(base_url=BASE) as c:
        r = await c.get("/static/css/style.css")
        assert r.status_code == 200
