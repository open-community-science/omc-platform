"""End-to-end workflow test: submission → interview → manuscript → review.

Tests the complete pipeline without HPC/SLURM (dev mode) and without
GitHub API (no token configured). Validates all internal data flows.
"""
from pathlib import Path
import sys
import re
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytestmark = pytest.mark.ai


# Drives a live portal over HTTP: skipped unless a dev instance is actually
# there (see tests/conftest.py).
pytestmark = pytest.mark.live_server

@pytest.mark.asyncio
@pytest.mark.timeout(300)
async def test_full_workflow(client):
    """Full workflow: create → interview → generate → review."""

    # Step 1: Create submission
    resp = await client.post("/submissions/create", data={
        "sra_accession": "PRJNA656268",
        "pipeline": "nanopore_mag",
        "title": "E2E Test: Cyanobacterial MAGs",
    }, follow_redirects=True)
    assert resp.status_code == 200

    # Extract submission ID
    sub_id = None
    if resp.url and "/submissions/" in str(resp.url):
        sub_id = int(str(resp.url).split("/submissions/")[-1].split("/")[0].split("?")[0])
    if not sub_id:
        resp2 = await client.get("/submissions", follow_redirects=True)
        subs = resp2.json()
        assert subs, "No submissions found"
        sub_id = subs[0]["id"]

    print(f"\n[1/5] Created submission {sub_id}")

    # Step 2: AI interview — send opening + one answer
    resp = await client.post(f"/interviews/{sub_id}/chat",
                             json={"message": ""})
    assert resp.status_code == 200
    data = resp.json()
    assert "response" in data
    print(f"[2/5] Interview started: {data['response'][:100]}...")

    resp = await client.post(f"/interviews/{sub_id}/chat",
                             json={"message": "We studied cyanobacterial blooms in Lake Erie using nanopore metagenomics. "
                                              "We expected to find Microcystis-dominated communities with toxin genes."})
    assert resp.status_code == 200
    data = resp.json()
    assert "response" in data
    print(f"      AI follow-up: {data['response'][:100]}...")

    # Step 3: Generate manuscript
    resp = await client.post(f"/reviews/{sub_id}/generate")
    assert resp.status_code == 200
    data = resp.json()
    assert "manuscript" in data
    sections = data["manuscript"]
    assert "introduction" in sections
    assert "methods" in sections
    assert "results" in sections
    assert "discussion" in sections
    assert "abstract" in sections
    print(f"[3/5] Manuscript generated: {len(sections)} sections")
    for name, content in sections.items():
        if name != "bibliography":
            print(f"      {name}: {len(content)} chars")

    # Step 4: Run AI reviews
    resp = await client.post(f"/reviews/{sub_id}/run")
    assert resp.status_code == 200
    data = resp.json()
    assert "reviews" in data
    reviews = data["reviews"]
    assert len(reviews) == 3  # statistical, methodological, clarity
    for r in reviews:
        rtype = r.get("review_type", "?")
        comments = r.get("comments", [])
        print(f"[4/5] {rtype} review: {len(comments)} comments")
        for c in comments[:2]:
            print(f"      [{c.get('severity','?')}] {c.get('issue','')[:80]}")

    # Step 5: Verify data persistence
    resp = await client.get(f"/submissions/{sub_id}", follow_redirects=True)
    assert resp.status_code == 200
    # The submission detail page should render without errors
    assert "E2E Test" in resp.text or "Cyanobacterial" in resp.text

    # Check interview data was stored
    resp = await client.get(f"/interviews/{sub_id}")
    assert resp.status_code == 200
    idata = resp.json()
    assert "answers" in idata or "chat_history" in idata or "_chat_history" in str(idata)

    print(f"[5/5] Data persistence verified")

    # Cleanup
    resp = await client.post(f"/submissions/{sub_id}/delete")
    print(f"      Cleaned up submission {sub_id}")
