"""Test manuscript generation and review via API endpoints."""
import sys
import re
import pytest

sys.path.insert(0, "/data/omc/omc-platform")

pytestmark = pytest.mark.ai


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_ai_interview_chat_api(client):
    """Test the AI interview chat endpoint via HTTP."""
    # Create a submission first
    resp = await client.post("/submissions/create", data={
        "sra_accession": "PRJNA656268",
        "pipeline": "nanopore_mag",
    }, follow_redirects=True)

    # Get submission ID from redirect URL or dashboard
    sub_id = None
    if resp.url and "/submissions/" in str(resp.url):
        sub_id = int(str(resp.url).split("/submissions/")[-1].split("/")[0])
    else:
        resp2 = await client.get("/dashboard", follow_redirects=True)
        matches = re.findall(r'submissions/(\d+)', resp2.text)
        assert matches, "No submissions found"
        sub_id = int(matches[0])

    # Start AI interview
    resp = await client.post(f"/interviews/{sub_id}/chat",
                             json={"message": ""})
    assert resp.status_code == 200
    data = resp.json()
    assert "response" in data
    assert len(data["response"]) > 20
    print(f"\nAI opening: {data['response'][:200]}...")

    # Send a message
    resp = await client.post(f"/interviews/{sub_id}/chat",
                             json={"message": "We're studying cyanobacterial blooms in freshwater."})
    assert resp.status_code == 200
    data = resp.json()
    assert "response" in data
    assert data["complete"] is False
    print(f"AI follow-up: {data['response'][:200]}...")

    # Cleanup
    await client.post(f"/submissions/{sub_id}/delete")
