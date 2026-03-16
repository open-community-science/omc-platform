"""Tool definitions and execution for OMC session chat.

Each tool is registered with an OpenAI-compatible function schema and an async handler.
The LLM calls tools via the standard tool_calls mechanism.
"""

import json
import os

import httpx

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://host.docker.internal:8002/api/llm")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "not-needed")


# ── Tool registry ────────────────────────────────────────────────────────────

# Each entry: {"schema": <OpenAI function tool schema>, "handler": <async callable>}
_registry: dict[str, dict] = {}


def tool(name: str, description: str, parameters: dict):
    """Decorator to register a tool with its OpenAI function schema."""
    def decorator(fn):
        _registry[name] = {
            "schema": {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                },
            },
            "handler": fn,
        }
        return fn
    return decorator


def get_tool_schemas() -> list[dict]:
    """Return all tool schemas for the OpenAI API tools parameter."""
    return [entry["schema"] for entry in _registry.values()]


async def execute_tool(name: str, args: dict) -> str:
    """Execute a tool by name and return the result as a JSON string."""
    entry = _registry.get(name)
    if not entry:
        return json.dumps({"error": f"Unknown tool: {name}"})
    try:
        result = await entry["handler"](**args)
        return json.dumps(result, indent=1, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Tool definitions ─────────────────────────────────────────────────────────

@tool(
    name="browse_samples",
    description=(
        "Browse sample metadata records with paging. Returns shared columns "
        "(identical across all samples) and varying columns per record. "
        "Use page/page_size to navigate large datasets, search to filter by keyword."
    ),
    parameters={
        "type": "object",
        "properties": {
            "page": {
                "type": "integer",
                "description": "Page number (default 1)",
                "default": 1,
            },
            "page_size": {
                "type": "integer",
                "description": "Records per page (default 50, max 100)",
                "default": 50,
            },
            "search": {
                "type": "string",
                "description": "Search keyword to filter records",
                "default": "",
            },
        },
    },
)
async def browse_samples(page: int = 1, page_size: int = 50, search: str = "") -> dict:
    """Browse sample metadata via the portal proxy."""
    page_size = min(page_size, 100)
    params = {"page": page, "page_size": page_size}
    if search:
        params["search"] = search
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{LLM_BASE_URL}/sample-metadata",
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                params=params,
            )
            result = resp.json()
    except Exception as e:
        return {"error": str(e)}

    if result.get("error"):
        return result

    return {
        "page": result["page"],
        "total_pages": result["total_pages"],
        "total_records": result["total"],
        "shared_columns": result["shared"],
        "varying_columns": result["varying_columns"],
        "records": result["records"],
    }
