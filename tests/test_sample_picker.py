"""Test sample picker / breakdown endpoint."""
import sys
import pytest

sys.path.insert(0, "/data/omc/omc-platform")


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_sample_breakdown(client):
    """Fetch sample breakdown for a BioProject with multiple data types."""
    resp = await client.get("/submissions/lookup/PRJNA656268/samples")
    assert resp.status_code == 200
    data = resp.json()

    assert "breakdown" in data
    assert data["num_runs"] > 0
    assert data["num_samples"] > 0
    assert data["accession"] == "PRJNA656268"

    print(f"\n{data['accession']}: {data['title']}")
    print(f"  Organism: {data['organism']}")
    print(f"  {data['num_runs']} runs, {data['num_samples']} samples")
    print(f"  Suggested pipeline: {data['suggested_pipeline']}")
    print(f"  Breakdown:")
    for b in data["breakdown"]:
        gb = round(b["bases"] / 1e9, 2) if b["bases"] else 0
        print(f"    {b['platform']} / {b['strategy']} / {b['layout']}: "
              f"{b['runs']} runs, {b['samples']} samples, {gb} Gb")


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_sample_breakdown_invalid(client):
    """Invalid accession returns error."""
    resp = await client.get("/submissions/lookup/INVALID123/samples")
    assert resp.status_code == 200
    data = resp.json()
    assert "error" in data
