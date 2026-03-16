"""LLM proxy endpoint tests."""
import httpx
import pytest

BASE = "http://127.0.0.1:8002"


@pytest.mark.asyncio
async def test_health_endpoint():
    """LLM proxy health check returns backend status."""
    async with httpx.AsyncClient(base_url=BASE) as c:
        r = await c.get("/api/llm/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] in ("ok", "unreachable")
        assert "backend" in data


@pytest.mark.asyncio
async def test_completions_rejects_no_auth():
    """Chat completions rejects requests without a valid session token."""
    async with httpx.AsyncClient(base_url=BASE) as c:
        r = await c.post(
            "/api/llm/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 401
        assert "session token" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_completions_rejects_bad_token():
    """Chat completions rejects requests with an invalid token."""
    async with httpx.AsyncClient(base_url=BASE) as c:
        r = await c.post(
            "/api/llm/chat/completions",
            headers={"Authorization": "Bearer fake-token-12345"},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_models_rejects_no_auth():
    """Models endpoint requires auth."""
    async with httpx.AsyncClient(base_url=BASE) as c:
        r = await c.get("/api/llm/models")
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_dev_create_token_and_completions():
    """Create a dev token and use it for a completion (requires DEBUG=true and LLM running)."""
    async with httpx.AsyncClient(base_url=BASE, timeout=30.0) as c:
        # Create token
        r = await c.post(
            "/api/llm/dev/create-token",
            json={"slug": "test-proxy"},
        )
        if r.status_code == 403:
            pytest.skip("Portal not running in debug mode")
        assert r.status_code == 200
        token = r.json()["token"]
        assert len(token) == 48

        # Use token for models endpoint
        r = await c.get(
            "/api/llm/models",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        assert r.json()["object"] == "list"

        # Use token for completion
        r = await c.post(
            "/api/llm/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "messages": [{"role": "user", "content": "Say OK"}],
                "max_tokens": 10,
            },
        )
        if r.status_code == 502:
            pytest.skip("LLM backend unreachable")
        assert r.status_code == 200
        assert "choices" in r.json()


@pytest.mark.asyncio
async def test_rate_limiting():
    """Verify rate limiting kicks in after threshold."""
    async with httpx.AsyncClient(base_url=BASE, timeout=10.0) as c:
        # Create token
        r = await c.post(
            "/api/llm/dev/create-token",
            json={"slug": "test-ratelimit"},
        )
        if r.status_code == 403:
            pytest.skip("Portal not running in debug mode")
        token = r.json()["token"]

        # Hit models endpoint rapidly (cheaper than completions)
        statuses = []
        for _ in range(35):
            r = await c.get(
                "/api/llm/models",
                headers={"Authorization": f"Bearer {token}"},
            )
            statuses.append(r.status_code)

        # Should see 429 after 30 requests
        assert 429 in statuses, "Rate limiting did not trigger after 30+ requests"
