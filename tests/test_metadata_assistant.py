"""Test metadata assistant with LM Studio fallback."""
import sys
import pytest

sys.path.insert(0, "/data/omc/omc-platform")
from ai.metadata_assistant import interactive_metadata_help, validate_metadata

pytestmark = pytest.mark.ai


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_metadata_help_session():
    """Test a single turn of metadata assistance via LM Studio."""
    result = await interactive_metadata_help(
        api_key="",  # empty = use LM Studio fallback
        user_message="I collected sediment samples from the Arctic Ocean near Svalbard on June 15, 2023. The samples are from 200m depth, temperature was -1.2C.",
        conversation_history=[],
        current_metadata={},
    )
    assert "response" in result
    assert isinstance(result["response"], str)
    assert len(result["response"]) > 20
    # Should extract some metadata
    print(f"\nResponse ({len(result['response'])} chars): {result['response'][:200]}...")
    print(f"Extracted: {result.get('extracted_metadata', {})}")


def test_validate_metadata_complete():
    meta = {
        "sample_name": "ARCTIC_SED_001",
        "organism": "marine sediment metagenome",
        "collection_date": "2023-06-15",
        "geo_loc_name": "Norway: Svalbard",
        "lat_lon": "78.23 15.63",
        "isolation_source": "marine sediment",
        "depth": "200",
        "temp": "-1.2",
    }
    result = validate_metadata(meta)
    assert result["valid"] is True
    assert len(result["errors"]) == 0


def test_validate_metadata_missing_required():
    meta = {"sample_name": "TEST"}
    result = validate_metadata(meta)
    assert result["valid"] is False
    assert len(result["errors"]) >= 2  # missing organism, collection_date, geo_loc_name


def test_validate_metadata_bad_date():
    meta = {
        "sample_name": "TEST",
        "organism": "marine metagenome",
        "collection_date": "June 2023",
        "geo_loc_name": "Arctic",
    }
    result = validate_metadata(meta)
    assert any("date" in e.lower() for e in result["errors"])
