"""Test AI conversational interview endpoint."""
import sys
import pytest

sys.path.insert(0, "/data/omc/omc-platform")


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_ai_interview_start():
    """Test starting an AI interview session."""
    from ai.author_interview import start_interview

    project_meta = {
        "accession": "PRJNA656268",
        "pipeline": "nanopore_mag",
        "title": "Cyanobacterial bloom metagenomics",
        "metadata": {"organism": "freshwater metagenome"},
    }

    opening = await start_interview(project_meta, "nanopore_mag")
    assert len(opening) > 50
    print(f"\nAI opening ({len(opening)} chars):")
    print(opening[:300])


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_ai_interview_turn():
    """Test a conversation turn in the AI interview."""
    from ai.author_interview import conduct_interview_turn

    project_meta = {
        "accession": "PRJNA656268",
        "pipeline": "nanopore_mag",
        "title": "Cyanobacterial bloom metagenomics",
    }

    history = [
        {"role": "assistant", "content": "Welcome! Tell me about your research question."},
    ]

    result = await conduct_interview_turn(
        "We're characterizing cyanobacterial communities in a freshwater bloom using nanopore long-read metagenomics.",
        history, project_meta, "nanopore_mag"
    )
    assert len(result["response"]) > 30
    assert isinstance(result["complete"], bool)
    print(f"\nAI response ({len(result['response'])} chars, complete={result['complete']}):")
    print(result["response"][:300])
