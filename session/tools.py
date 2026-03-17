"""Tool definitions and execution for OMC session chat.

Each tool is registered with an OpenAI-compatible function schema and an async handler.
The LLM calls tools via the standard tool_calls mechanism.
"""

import csv
import gzip
import io
import json
import os
from pathlib import Path

import httpx

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://host.docker.internal:8002/api/llm")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "not-needed")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
VIZ_DATA_DIR = Path("/app/viz/data")


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


# ── Pipeline data tools ─────────────────────────────────────────────────────

@tool(
    name="list_data_files",
    description=(
        "List pipeline result files available in /data. Returns a tree of directories "
        "and files with sizes. Use this to discover what pipeline outputs are available "
        "before reading specific files."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Subdirectory to list (e.g. 'assembly', 'binning/dastool'). Empty for root.",
                "default": "",
            },
        },
    },
)
async def list_data_files(path: str = "") -> dict:
    """List files in the pipeline results directory."""
    target = DATA_DIR / path
    if not target.exists():
        return {"error": f"Path not found: {path}"}
    if not str(target.resolve()).startswith(str(DATA_DIR.resolve())):
        return {"error": "Access denied"}

    entries = []
    try:
        for item in sorted(target.iterdir()):
            if item.name.startswith("."):
                continue
            if item.is_dir():
                # Count files recursively
                n_files = sum(1 for _ in item.rglob("*") if _.is_file())
                entries.append({"name": item.name + "/", "type": "dir", "files": n_files})
            else:
                size = item.stat().st_size
                if size > 1_000_000:
                    size_str = f"{size / 1_000_000:.1f}MB"
                elif size > 1000:
                    size_str = f"{size / 1000:.1f}KB"
                else:
                    size_str = f"{size}B"
                entries.append({"name": item.name, "type": "file", "size": size_str})
    except PermissionError:
        return {"error": "Permission denied"}

    return {"path": path or "/data", "entries": entries}


def _read_tsv(file_path: Path, max_rows: int) -> dict:
    """Read a TSV/CSV file and return structured data."""
    opener = gzip.open if file_path.suffix == ".gz" else open
    mode = "rt"
    delimiter = "\t" if ".tsv" in file_path.name else ","

    with opener(file_path, mode) as f:
        reader = csv.reader(f, delimiter=delimiter)
        try:
            headers = next(reader)
        except StopIteration:
            return {"headers": [], "rows": [], "total_rows": 0}

        rows = []
        total = 0
        for row in reader:
            total += 1
            if len(rows) < max_rows:
                rows.append(row)

    return {"headers": headers, "rows": rows, "total_rows": total, "truncated": total > max_rows}


@tool(
    name="read_data_file",
    description=(
        "Read a pipeline result file. Supports JSON, TSV, CSV, and text files. "
        "For tabular files (TSV/CSV), returns headers + rows with optional row limit. "
        "For JSON files, returns parsed content. For text files, returns lines. "
        "Use list_data_files first to discover available files."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to /data (e.g. 'assembly/assembly_info.txt', 'binning/dastool/summary.tsv')",
            },
            "max_rows": {
                "type": "integer",
                "description": "Max rows to return for tabular files (default 50, max 500)",
                "default": 50,
            },
        },
        "required": ["path"],
    },
)
async def read_data_file(path: str, max_rows: int = 50) -> dict:
    """Read a data file from the pipeline results."""
    file_path = DATA_DIR / path
    if not file_path.exists():
        return {"error": f"File not found: {path}"}
    if not str(file_path.resolve()).startswith(str(DATA_DIR.resolve())):
        return {"error": "Access denied"}
    if file_path.is_dir():
        return {"error": "Path is a directory, use list_data_files instead"}

    size = file_path.stat().st_size
    max_rows = min(max_rows, 500)

    # Binary files
    if file_path.suffix in (".bam", ".bai", ".fa", ".fasta", ".fna", ".sqsh", ".gz"):
        if ".tsv.gz" not in file_path.name and ".csv.gz" not in file_path.name and ".json.gz" not in file_path.name:
            return {"error": f"Binary file ({file_path.suffix}), not readable as text", "size": size}

    name = file_path.name.lower()

    # JSON
    if name.endswith(".json") or name.endswith(".json.gz"):
        try:
            opener = gzip.open if name.endswith(".gz") else open
            with opener(file_path, "rt") as f:
                data = json.load(f)
            # Truncate large arrays
            if isinstance(data, list) and len(data) > max_rows:
                return {"data": data[:max_rows], "total_items": len(data), "truncated": True}
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, list) and len(v) > max_rows:
                        data[k] = v[:max_rows]
                        data[f"_{k}_total"] = len(v)
                        data[f"_{k}_truncated"] = True
            return {"data": data}
        except (json.JSONDecodeError, OSError) as e:
            return {"error": f"Failed to parse JSON: {e}"}

    # TSV/CSV
    if ".tsv" in name or ".csv" in name:
        try:
            return _read_tsv(file_path, max_rows)
        except Exception as e:
            return {"error": f"Failed to parse tabular file: {e}"}

    # Plain text
    try:
        lines = file_path.read_text(errors="replace").splitlines()
        total = len(lines)
        return {"lines": lines[:max_rows], "total_lines": total, "truncated": total > max_rows}
    except Exception as e:
        return {"error": f"Failed to read file: {e}"}


@tool(
    name="get_results_summary",
    description=(
        "Get preprocessed pipeline result summaries. These are structured JSON files "
        "prepared by the danaSeq viz preprocessor, containing assembly stats, MAG quality, "
        "taxonomy, coverage, gene annotations, MGE detection, and more. "
        "Available datasets depend on which pipeline steps produced results."
    ),
    parameters={
        "type": "object",
        "properties": {
            "dataset": {
                "type": "string",
                "description": "Which summary to load",
                "enum": [
                    "overview", "mags", "contig_lengths", "taxonomy_sunburst",
                    "kegg_heatmap", "coverage", "mge_summary", "mge_per_bin",
                    "eukaryotic", "biosynthetic", "phylotree",
                ],
            },
        },
        "required": ["dataset"],
    },
)
async def get_results_summary(dataset: str) -> dict:
    """Read a preprocessed viz summary JSON file."""
    json_path = VIZ_DATA_DIR / f"{dataset}.json"
    gz_path = VIZ_DATA_DIR / f"{dataset}.json.gz"

    target = None
    if gz_path.exists():
        target = gz_path
    elif json_path.exists():
        target = json_path

    if not target:
        # List what IS available
        available = [
            p.stem.replace(".json", "")
            for p in VIZ_DATA_DIR.glob("*.json")
        ] if VIZ_DATA_DIR.exists() else []
        return {"error": f"Dataset '{dataset}' not available", "available": available}

    try:
        opener = gzip.open if target.suffix == ".gz" else open
        with opener(target, "rt") as f:
            data = json.load(f)
        # For large arrays, summarize
        if isinstance(data, list) and len(data) > 100:
            return {"data": data[:100], "total_items": len(data), "truncated": True}
        return {"data": data}
    except Exception as e:
        return {"error": f"Failed to load {dataset}: {e}"}
