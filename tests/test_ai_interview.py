"""Test AI author interview with real LM Studio inference."""
import sys
import pytest

sys.path.insert(0, "/data/omc/omc-platform")
from ai.author_interview import start_interview, conduct_interview_turn

MOCK_METADATA = {
    "accession": "PRJNA656268",
    "title": "Marine metagenome study",
    "organism": "marine metagenome",
    "num_sra_runs": 500,
    "platform": "ILLUMINA",
    "library_strategy": "AMPLICON",
}


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_start_interview():
    """AI should introduce itself and ask a question based on metadata."""
    resp = await start_interview(MOCK_METADATA, "illumina_mag")
    assert isinstance(resp, str)
    assert len(resp) > 20
    # Should reference something from the metadata
    assert any(w in resp.lower() for w in ["marine", "metagenome", "amplicon", "illumina", "500"])


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_interview_turn():
    """AI should respond to author and track conversation."""
    history = []
    result = await conduct_interview_turn(
        "We're studying how temperature affects marine microbial diversity across ocean basins.",
        history,
        MOCK_METADATA,
        "illumina_mag",
    )
    assert "response" in result
    assert isinstance(result["response"], str)
    assert len(result["response"]) > 20
    assert isinstance(result["complete"], bool)
