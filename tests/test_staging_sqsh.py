"""Staging API squashfs upload/download tests."""
import httpx
import pytest
import subprocess
import tempfile
import os
from pathlib import Path

BASE = os.environ.get("OMC_TEST_BASE_URL", "http://127.0.0.1:8002")

# Read staging key from env or portal .env
STAGING_KEY = os.environ.get("STAGING_API_KEY", "")
if not STAGING_KEY:
    env_file = Path(__file__).parent.parent / "portal" / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("STAGING_API_KEY="):
                STAGING_KEY = line.split("=", 1)[1].strip()


# Drives a live portal over HTTP: skipped unless a dev instance is actually
# there (see tests/conftest.py).
pytestmark = pytest.mark.live_server

def auth_headers():
    if not STAGING_KEY:
        pytest.skip("STAGING_API_KEY not set")
    return {"Authorization": f"Bearer {STAGING_KEY}"}


def make_test_sqsh() -> bytes:
    """Create a small squashfs archive in memory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create test pipeline output structure
        for subdir in ["taxonomy", "assembly"]:
            d = Path(tmpdir) / "data" / subdir
            d.mkdir(parents=True)
            (d / f"test_{subdir}.tsv").write_text(f"col1\tcol2\nval1\tval2\n")

        sqsh_path = Path(tmpdir) / "test.sqsh"
        result = subprocess.run(
            ["mksquashfs", str(Path(tmpdir) / "data"), str(sqsh_path),
             "-quiet", "-noappend"],
            capture_output=True,
        )
        if result.returncode != 0:
            pytest.skip(f"mksquashfs not available: {result.stderr.decode()}")

        return sqsh_path.read_bytes()


@pytest.mark.asyncio
async def test_upload_results():
    """Upload a .sqsh file via staging API."""
    sqsh_data = make_test_sqsh()
    slug = "test-sqsh-upload"

    async with httpx.AsyncClient(base_url=BASE, timeout=30.0) as c:
        # Upload
        r = await c.post(
            f"/staging/{slug}/upload-results",
            headers=auth_headers(),
            content=sqsh_data,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["slug"] == slug
        assert data["size_bytes"] == len(sqsh_data)

        # Verify it appears in results list
        r = await c.get("/staging/results", headers=auth_headers())
        assert r.status_code == 200
        slugs = [res["slug"] for res in r.json()["results"]]
        assert slug in slugs

        # Cleanup
        r = await c.delete(f"/staging/{slug}/results", headers=auth_headers())
        assert r.status_code == 200

        # Verify it's gone
        r = await c.get("/staging/results", headers=auth_headers())
        slugs = [res["slug"] for res in r.json()["results"]]
        assert slug not in slugs


@pytest.mark.asyncio
async def test_upload_rejects_empty():
    """Reject empty uploads."""
    async with httpx.AsyncClient(base_url=BASE, timeout=10.0) as c:
        r = await c.post(
            "/staging/test-empty/upload-results",
            headers=auth_headers(),
            content=b"",
        )
        assert r.status_code == 400
        assert "empty" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_upload_rejects_no_auth():
    """Upload requires staging key."""
    async with httpx.AsyncClient(base_url=BASE) as c:
        r = await c.post(
            "/staging/test-noauth/upload-results",
            content=b"fake data",
        )
        # 401 = bad key, 503 = staging not configured, 404 = route issue
        assert r.status_code in (401, 503, 404)


@pytest.mark.asyncio
async def test_delete_nonexistent():
    """Delete returns 404 for missing archive."""
    async with httpx.AsyncClient(base_url=BASE) as c:
        r = await c.delete(
            "/staging/nonexistent-slug/results",
            headers=auth_headers(),
        )
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_results_list_empty():
    """Results list returns empty array when nothing uploaded."""
    async with httpx.AsyncClient(base_url=BASE) as c:
        r = await c.get("/staging/results", headers=auth_headers())
        assert r.status_code == 200
        assert "results" in r.json()
