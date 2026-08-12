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


def _available_datasets() -> list[str]:
    """Dataset names actually present in the viz data dir (any *.json[.gz])."""
    if not VIZ_DATA_DIR.exists():
        return []
    names = set()
    for p in VIZ_DATA_DIR.glob("*.json*"):
        if p.suffix in (".json", ".gz"):
            names.add(p.name.split(".json")[0])
    return sorted(names)


def _load_viz_json(name: str):
    """Load a viz JSON by base name, trying .json then .json.gz. None if absent."""
    for cand in (VIZ_DATA_DIR / f"{name}.json", VIZ_DATA_DIR / f"{name}.json.gz"):
        if cand.exists():
            opener = gzip.open if cand.suffix == ".gz" else open
            with opener(cand, "rt") as f:
                return json.load(f)
    return None


def _synthesize_overview() -> dict:
    """Build an overview from whatever viz files exist.

    The MAG viz emits an `overview.json`; the amplicon viz does not
    — it emits renorm_stats/taxonomy/samples/provenance/network. Rather than
    return an error for the model's very first call, synthesise a compact
    overview from the files that are present so both pipeline types work.
    """
    ov = {}
    # Amplicon signals
    renorm = _load_viz_json("renorm_stats")
    if isinstance(renorm, dict):
        ov["pipeline_kind"] = "amplicon (ASV)"
        ov["renorm"] = renorm
    samples = _load_viz_json("samples")
    if isinstance(samples, list):
        ov["n_samples"] = len(samples)
        ov["total_reads"] = sum(s.get("total_reads", 0) for s in samples if isinstance(s, dict))
    tax = _load_viz_json("taxonomy")
    if isinstance(tax, dict) and tax:
        db, body = next(iter(tax.items()))
        if isinstance(body, dict):
            assigns = body.get("assignments", {})
            levels = body.get("levels", [])
            ov["taxonomy_db"] = db
            ov["n_asvs_classified"] = len(assigns)
            if levels and assigns:
                pidx = levels.index("Phylum") if "Phylum" in levels else 1
                from collections import Counter
                phyla = Counter(a[pidx] for a in assigns.values()
                                if isinstance(a, list) and pidx < len(a) and a[pidx])
                ov["top_phyla"] = dict(phyla.most_common(5))
    prov = _load_viz_json("provenance")
    if isinstance(prov, dict) and prov.get("total"):
        t = prov["total"]
        stages = prov.get("stages", [])
        first = stages[0]["id"] if stages else None
        last = stages[-1]["id"] if stages else None
        if first in t and last in t and t.get(first):
            ov["read_retention"] = {
                "input": t[first], "retained": t[last],
                "pct": round(100 * t[last] / t[first], 1),
            }
        ov["n_samples_provenance"] = len(prov.get("samples", {}))
    # If nothing amplicon-ish was found, say what's available for MAG runs.
    if not ov:
        return {"error": "No overview available", "available_datasets": _available_datasets()}
    ov["available_datasets"] = _available_datasets()
    return ov


@tool(
    name="get_results_summary",
    description=(
        "Get preprocessed pipeline result summaries (structured JSON from the danaSeq "
        "viz preprocessor). The available datasets depend on the pipeline type:\n"
        "- Amplicon (16S/18S): overview, taxonomy, samples, heatmap, "
        "aggregated_counts, asvs, counts, network, renorm_stats, provenance\n"
        "- Metagenome (MAG): overview, mags, contig_lengths, taxonomy_sunburst, "
        "kegg_heatmap, coverage, mge_summary, eukaryotic, biosynthetic, phylotree\n"
        "Always start with 'overview' — it is synthesised from whatever exists, so it "
        "works for either pipeline. Call with an unknown name to see the real available list."
    ),
    parameters={
        "type": "object",
        "properties": {
            "dataset": {
                "type": "string",
                "description": (
                    "Which summary to load. Use 'overview' first. "
                    "Then any name from the available list for this submission."
                ),
            },
        },
        "required": ["dataset"],
    },
)
async def get_results_summary(dataset: str) -> dict:
    """Read a preprocessed viz summary JSON file (synthesises 'overview' if absent)."""
    json_path = VIZ_DATA_DIR / f"{dataset}.json"
    gz_path = VIZ_DATA_DIR / f"{dataset}.json.gz"

    target = None
    if gz_path.exists():
        target = gz_path
    elif json_path.exists():
        target = json_path

    if not target:
        # 'overview' is special: synthesise it from whatever files are present so
        # the model's instructed first call never dead-ends (amplicon has no
        # overview.json). Any other missing name returns the real available list.
        if dataset == "overview":
            return {"data": _synthesize_overview()}
        return {"error": f"Dataset '{dataset}' not available",
                "available": _available_datasets()}

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
