"""Session manager tests.

These test the session lifecycle without actually launching Docker containers.
Tests that require Docker are marked with @pytest.mark.docker.
"""
import os
import httpx
import pytest

BASE = os.environ.get("OMC_TEST_BASE_URL", "http://127.0.0.1:8002")


# Drives a live portal over HTTP: skipped unless a dev instance is actually
# there (see tests/conftest.py).
pytestmark = pytest.mark.live_server

@pytest.mark.asyncio
async def test_sessions_list_requires_auth():
    """Session list requires authentication."""
    async with httpx.AsyncClient(base_url=BASE) as c:
        r = await c.get("/sessions/")
        # Should redirect to login or return 401/403
        assert r.status_code in (302, 401, 403)


@pytest.mark.asyncio
async def test_session_launch_requires_auth():
    """Session launch requires authentication."""
    async with httpx.AsyncClient(base_url=BASE) as c:
        r = await c.post("/sessions/nonexistent/launch")
        assert r.status_code in (302, 401, 403)


@pytest.mark.asyncio
async def test_session_stop_requires_auth():
    """Session stop requires authentication."""
    async with httpx.AsyncClient(base_url=BASE) as c:
        r = await c.post("/sessions/nonexistent/stop")
        assert r.status_code in (302, 401, 403)


@pytest.mark.asyncio
async def test_session_chat_page_no_session():
    """Chat page shows launch UI or auth error when no session is running."""
    async with httpx.AsyncClient(base_url=BASE, follow_redirects=True) as c:
        await c.get("/auth/login", follow_redirects=True)
        r = await c.get("/sessions/nonexistent/chat")
        # 200 = launch page, 401 = auth required (session cookie issue), 404 = not found
        assert r.status_code in (200, 401, 404)


@pytest.mark.asyncio
async def test_session_launch_nonexistent_submission():
    """Launching a session for a nonexistent submission returns 401 or 404."""
    async with httpx.AsyncClient(base_url=BASE, follow_redirects=False) as c:
        await c.get("/auth/login", follow_redirects=True)
        r = await c.post("/sessions/nonexistent-slug/launch")
        # 401 if auth cookie not persisted, 404 if auth works but slug not found
        assert r.status_code in (401, 404)


@pytest.mark.asyncio
async def test_session_notebook_no_session():
    """Notebook redirect returns 401 or 404 when no session is running."""
    async with httpx.AsyncClient(base_url=BASE, follow_redirects=False) as c:
        await c.get("/auth/login", follow_redirects=True)
        r = await c.get("/sessions/nonexistent/notebook")
        assert r.status_code in (401, 404)


# ── Unit tests for session internals ─────────────────────────────────────


def test_port_allocation():
    """Port allocator returns consecutive triples and detects exhaustion."""
    from portal.app.sessions import _allocate_ports, _release_ports, _used_ports

    # Save state
    saved = _used_ports.copy()
    _used_ports.clear()

    try:
        p1, p2, p3 = _allocate_ports()
        assert p2 == p1 + 1
        assert p3 == p1 + 2
        assert p1 in _used_ports
        assert p2 in _used_ports
        assert p3 in _used_ports

        p4, p5, p6 = _allocate_ports()
        assert p4 == p3 + 1
        assert p4 != p1

        _release_ports(p1, p2, p3)
        assert p1 not in _used_ports
        assert p2 not in _used_ports
        assert p3 not in _used_ports
    finally:
        _used_ports.clear()
        _used_ports.update(saved)


def test_session_token_lifecycle():
    """Session tokens can be created and revoked."""
    from portal.app.llm_proxy import (
        create_session_token, revoke_session_token, _session_tokens,
    )

    token = create_session_token("test-lifecycle")
    assert token in _session_tokens
    assert _session_tokens[token]["slug"] == "test-lifecycle"
    assert len(token) == 48

    revoke_session_token("test-lifecycle")
    assert token not in _session_tokens


def test_session_token_uniqueness():
    """Each call creates a unique token."""
    from portal.app.llm_proxy import create_session_token, revoke_session_token

    t1 = create_session_token("test-unique-1")
    t2 = create_session_token("test-unique-2")
    assert t1 != t2

    revoke_session_token("test-unique-1")
    revoke_session_token("test-unique-2")


def test_results_format_enum():
    """ResultsFormat enum has expected values."""
    from portal.app.database import ResultsFormat

    assert ResultsFormat.NONE.value == "none"
    assert ResultsFormat.LIVE.value == "live"
    assert ResultsFormat.ARCHIVED.value == "archived"
    assert ResultsFormat.TRANSFERRED.value == "transferred"
