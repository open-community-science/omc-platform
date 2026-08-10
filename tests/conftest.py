"""Shared test fixtures.

The suite mixes two kinds of test. Most are pure unit tests that run anywhere.
The rest drive a **live portal** over HTTP, and those need care on two counts
(issue #52):

1. Without a server they failed with `httpx.ConnectError`, which reads as broken
   code rather than an unconfigured environment. They are marked `live_server`
   and now skip when nothing is listening.

2. They target whatever answers on the configured port — and several of them
   *write*: creating and deleting submissions, posting to `/staging/`. Run on a
   host where that port belongs to a real deployment, the suite drives that
   deployment. So the probe below also refuses to run them against anything that
   is not a dev instance, regardless of what is listening.

Point the suite elsewhere with `OMC_TEST_BASE_URL=http://127.0.0.1:9000`.
"""
import os

import httpx
import pytest
import pytest_asyncio

BASE = os.environ.get("OMC_TEST_BASE_URL", "http://127.0.0.1:8002")

_probe_result = None  # cached (reachable, is_dev, detail) for the whole session


def probe_server() -> tuple[bool, bool, str]:
    """Is BASE a reachable *dev* portal? Probed once per session."""
    global _probe_result
    if _probe_result is not None:
        return _probe_result

    try:
        r = httpx.get(f"{BASE}/auth/login", follow_redirects=False, timeout=3.0)
    except httpx.HTTPError as exc:
        _probe_result = (False, False, f"no portal at {BASE} ({type(exc).__name__})")
        return _probe_result

    # In dev mode /auth/login auto-logs in and lands on /dashboard. A real
    # deployment has GitHub OAuth configured and sends you to github.com. Only
    # the former is safe to create and delete submissions in.
    location = r.headers.get("location", "")
    is_dev = r.status_code in (302, 303) and "github.com" not in location
    detail = (
        f"dev portal at {BASE}" if is_dev
        else f"{BASE} is not a dev instance (/auth/login -> "
             f"{location[:60] or r.status_code}); refusing to run "
             "write-capable tests against it"
    )
    _probe_result = (True, is_dev, detail)
    return _probe_result


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_server: needs a running dev portal (see OMC_TEST_BASE_URL)",
    )


def pytest_collection_modifyitems(config, items):
    """Skip live_server tests unless a dev portal is actually there."""
    marked = [i for i in items if i.get_closest_marker("live_server")]
    if not marked:
        return

    reachable, is_dev, detail = probe_server()
    if reachable and is_dev:
        return

    skip = pytest.mark.skip(reason=detail)
    for item in marked:
        item.add_marker(skip)


@pytest_asyncio.fixture
async def client():
    """Authenticated httpx client with generous timeout for LLM calls."""
    timeout = httpx.Timeout(120.0, connect=10.0)
    async with httpx.AsyncClient(base_url=BASE, follow_redirects=False, timeout=timeout) as c:
        # Dev mode auto-login
        await c.get("/auth/login", follow_redirects=True)
        yield c
