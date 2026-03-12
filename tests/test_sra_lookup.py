"""SRA metadata lookup with real NCBI data."""
import asyncio
import httpx
import pytest

BASE = "http://127.0.0.1:8002"


@pytest.mark.asyncio
async def test_bioproject_lookup():
    """Lookup a real marine metagenome BioProject."""
    async with httpx.AsyncClient(base_url=BASE) as c:
        r = await c.get("/submissions/lookup/PRJNA656268")
        # Retry once if rate-limited
        if r.status_code == 200 and "error" in r.json():
            await asyncio.sleep(3)
            r = await c.get("/submissions/lookup/PRJNA656268")

        assert r.status_code == 200
        data = r.json()
        assert data["accession"] == "PRJNA656268"
        assert data["organism"] is not None
        assert len(data.get("breakdown", [])) > 0
        assert data.get("num_sra_runs", 0) > 0


@pytest.mark.asyncio
async def test_invalid_accession():
    async with httpx.AsyncClient(base_url=BASE) as c:
        r = await c.get("/submissions/lookup/INVALID123")
        assert "error" in r.json()
