"""Shared test fixtures."""
import httpx
import pytest
import pytest_asyncio

BASE = "http://127.0.0.1:8002"


@pytest_asyncio.fixture
async def client():
    """Authenticated httpx client with generous timeout for LLM calls."""
    timeout = httpx.Timeout(120.0, connect=10.0)
    async with httpx.AsyncClient(base_url=BASE, follow_redirects=False, timeout=timeout) as c:
        # Dev mode auto-login
        await c.get("/auth/login", follow_redirects=True)
        yield c
