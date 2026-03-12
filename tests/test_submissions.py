"""Submission CRUD with real accession data."""
import asyncio
import httpx
import pytest

BASE = "http://127.0.0.1:8002"


@pytest.fixture
def auth_client():
    """Sync fixture returning an async context manager."""
    return httpx.AsyncClient(base_url=BASE, follow_redirects=False)


@pytest.mark.asyncio
async def test_create_and_delete_submission():
    async with httpx.AsyncClient(base_url=BASE, follow_redirects=False) as c:
        # Login
        await c.get("/auth/login", follow_redirects=True)

        # Wait to avoid NCBI rate limit
        await asyncio.sleep(1)

        # Create
        r = await c.post(
            "/submissions/create",
            data={"sra_accession": "PRJNA656268", "pipeline": "illumina_mag"},
        )
        assert r.status_code == 302
        sub_id = r.headers["location"].split("/")[-1]

        # Check status
        r = await c.get(f"/submissions/{sub_id}/status")
        assert r.status_code == 200
        assert r.json()["status"] == "draft"

        # List
        r = await c.get("/submissions")
        assert r.status_code == 200
        assert any(s["id"] == int(sub_id) for s in r.json())

        # Delete
        r = await c.post(f"/submissions/{sub_id}/delete")
        assert r.status_code == 302


@pytest.mark.asyncio
async def test_submit_to_hpc_dev_mode():
    """In dev mode (SLURM disabled), submission transitions to 'submitted'."""
    async with httpx.AsyncClient(base_url=BASE, follow_redirects=False) as c:
        await c.get("/auth/login", follow_redirects=True)
        await asyncio.sleep(1)

        r = await c.post(
            "/submissions/create",
            data={"sra_accession": "PRJNA656268", "pipeline": "illumina_mag"},
        )
        sub_id = r.headers["location"].split("/")[-1]

        # Submit
        r = await c.post(f"/submissions/{sub_id}/submit")
        assert r.status_code == 302

        r = await c.get(f"/submissions/{sub_id}/status")
        assert r.json()["status"] == "submitted"

        # Cleanup
        await c.post(f"/submissions/{sub_id}/delete")
