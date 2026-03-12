"""Server health checks."""
import httpx
import pytest

BASE = "http://127.0.0.1:8002"


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
