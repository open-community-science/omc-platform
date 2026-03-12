"""AI assistant for helping users prepare SRA/GenBank metadata submissions."""
from typing import Optional
from .llm_client import get_client, multi_turn


METADATA_FIELDS = {
    "sample_name": "Unique identifier for the sample",
    "organism": "Scientific name of the organism/community",
    "collection_date": "When the sample was collected (YYYY-MM-DD)",
    "geo_loc_name": "Geographic location (Country: Region: Locality)",
    "lat_lon": "Latitude and longitude (decimal degrees)",
    "isolation_source": "Description of where sample was isolated from",
    "env_broad_scale": "Broad environmental context (ENVO term)",
    "env_local_scale": "Local environmental context (ENVO term)",
    "env_medium": "Environmental medium (ENVO term)",
    "host": "Host organism if applicable",
    "depth": "Depth of sample collection (meters)",
    "elev": "Elevation of sample collection (meters)",
    "temp": "Temperature at collection (Celsius)",
    "ph": "pH at collection",
}


def _use_anthropic(api_key: str | None) -> bool:
    """Check if we should use Anthropic SDK (has key and SDK available)."""
    if not api_key:
        return False
    try:
        from anthropic import Anthropic  # noqa: F401
        return True
    except ImportError:
        return False


async def interactive_metadata_help(
    api_key: str,
    user_message: str,
    conversation_history: list,
    current_metadata: dict,
    base_url: str | None = None,
    model: str | None = None,
) -> dict:
    """
    Interactive chat assistant for metadata preparation.

    Returns response text and any extracted metadata values.
    Uses Anthropic API if api_key provided, otherwise falls back to LM Studio.
    """
    system_prompt = f"""You are a helpful assistant for preparing NCBI SRA/GenBank metadata submissions.
Your job is to help researchers provide accurate, complete metadata for their sequence submissions.

REQUIRED METADATA FIELDS:
{_format_fields(METADATA_FIELDS)}

CURRENT METADATA VALUES:
{_format_current(current_metadata)}

Guidelines:
- Ask clarifying questions if information is ambiguous
- Suggest ENVO ontology terms when appropriate
- Validate formats (dates, coordinates, etc.)
- Be encouraging but thorough
- Extract metadata values from natural language responses
- Flag any inconsistencies or missing required fields

When the user provides information, extract it into the appropriate field.
End your response with a JSON block if you've extracted any values:
```json
{{"field_name": "extracted_value"}}
```
"""

    messages = conversation_history + [{"role": "user", "content": user_message}]

    if _use_anthropic(api_key):
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            system=system_prompt,
            messages=messages,
        )
        response_text = response.content[0].text
    else:
        # Fall back to LM Studio / OpenAI-compatible endpoint
        client = get_client(base_url=base_url)
        response_text = multi_turn(
            client, system_prompt, messages,
            model=model, max_tokens=1500, temperature=0.7,
        )

    # Extract any JSON metadata from response
    extracted = {}
    if "```json" in response_text:
        try:
            json_start = response_text.index("```json") + 7
            json_end = response_text.index("```", json_start)
            import json
            extracted = json.loads(response_text[json_start:json_end])
        except (ValueError, json.JSONDecodeError):
            pass

    return {
        "response": response_text,
        "extracted_metadata": extracted,
        "conversation_history": messages + [{"role": "assistant", "content": response_text}],
    }


def validate_metadata(metadata: dict) -> dict:
    """Validate metadata and return errors/warnings."""
    errors = []
    warnings = []

    required = ["sample_name", "organism", "collection_date", "geo_loc_name"]
    for field in required:
        if field not in metadata or not metadata[field]:
            errors.append(f"Missing required field: {field}")

    # Validate date format
    if "collection_date" in metadata:
        import re
        date = metadata["collection_date"]
        if not re.match(r"^\d{4}(-\d{2}(-\d{2})?)?$", date):
            errors.append("collection_date should be YYYY, YYYY-MM, or YYYY-MM-DD")

    # Validate coordinates
    if "lat_lon" in metadata:
        import re
        coords = metadata["lat_lon"]
        if not re.match(r"^-?\d+\.?\d*\s+-?\d+\.?\d*$", coords):
            warnings.append("lat_lon should be 'latitude longitude' in decimal degrees")

    return {"errors": errors, "warnings": warnings, "valid": len(errors) == 0}


def _format_fields(fields: dict) -> str:
    """Format field definitions for prompt."""
    return "\n".join(f"- {name}: {desc}" for name, desc in fields.items())


def _format_current(metadata: dict) -> str:
    """Format current metadata values."""
    if not metadata:
        return "None provided yet"
    return "\n".join(f"- {k}: {v}" for k, v in metadata.items())
