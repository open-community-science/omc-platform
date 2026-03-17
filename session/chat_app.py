"""OMC Research Assistant — Chainlit chat app for author sessions.

Drives the author through interview → results review → figure workshop → manuscript.
Connects to an OpenAI-compatible LLM endpoint.
"""

import os
import json
from datetime import datetime, timezone
from pathlib import Path

import chainlit as cl
import chainlit.data as cl_data
from openai import OpenAI

from tools import get_tool_schemas, execute_tool
from data_layer import JSONDataLayer

# ── Data persistence + auth (enables sidebar with multiple threads) ───────────

cl_data._data_layer = JSONDataLayer()


@cl.header_auth_callback
def header_auth_callback(headers: dict) -> cl.User | None:
    """Auto-authenticate — containers are single-user, no login needed."""
    return cl.User(identifier="author", metadata={"role": "author"})

# ── Config from environment (set by portal when launching container) ─────────

# When launched by the portal, LLM_BASE_URL points to the portal's LLM proxy
# (e.g., http://172.30.0.1:8002/api/llm) and LLM_API_KEY is a session token.
# The OpenAI SDK appends /chat/completions automatically.
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://host.docker.internal:8002/api/llm")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "not-needed")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen/qwen3.5-35b-a3b")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
MARIMO_URL = os.environ.get("MARIMO_URL", "http://localhost:8081")

# ── Chat state persistence ────────────────────────────────────────────────────
STATE_FILE = Path("/app/.omc/chat_state.json")


def _save_state():
    """Write session state to container-local file after every message."""
    state = {
        "version": 1,
        "phase": cl.user_session.get("phase", "interview"),
        "history": cl.user_session.get("history", []),
        "interview_summary": cl.user_session.get("interview_summary", ""),
        "results_summary": cl.user_session.get("results_summary", ""),
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))

    # Also persist phase/summary in Chainlit thread metadata (for sidebar resume)
    try:
        thread_id = cl.context.session.thread_id
        if thread_id:
            import asyncio
            layer = cl_data.get_data_layer()
            if layer:
                asyncio.get_event_loop().create_task(
                    layer.update_thread(thread_id, metadata={
                        "phase": state["phase"],
                        "interview_summary": state["interview_summary"],
                        "results_summary": state["results_summary"],
                    })
                )
    except Exception:
        pass  # Non-critical


def _load_state() -> dict | None:
    """Load session state from container-local file if it exists."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return None
    return None


# Load metadata from file (round-trips with the data through the pipeline)
# Falls back to SUBMISSION_META env var for backwards compat
_meta_file = DATA_DIR / "metadata.json"
if not _meta_file.exists():
    _meta_file = Path("/metadata/metadata.json")
if _meta_file.exists():
    SUBMISSION_META = _meta_file.read_text()
else:
    SUBMISSION_META = os.environ.get("SUBMISSION_META", "{}")

# ── Phases ───────────────────────────────────────────────────────────────────

PHASES = ["interview", "results_review", "figure_workshop", "manuscript"]

SYSTEM_PROMPTS = {
    "interview": """You are the OMC Research Assistant — an AI collaborator that helps
researchers turn their sequencing data into published manuscripts on the Open Microbial
Community platform.

CURRENT STATUS: {status_context}

Your style is warm, curious, and unhurried. Think of this as a conversation over coffee,
not a form to fill out. You're genuinely interested in their work and who they are.

WHAT YOU KNOW:
You have the researcher's project metadata and per-sample metadata below. You can see
their sequencing runs, sampling locations, dates, organisms, and environmental context.
You also know which runs they've selected for analysis (if any).

YOUR ROLE IN THE WORKFLOW:
The OMC platform takes the researcher through these steps:
1. Data selection — choose which sequencing runs to analyze
2. Interview — this conversation! Get to know the researcher and their science
3. Pipeline analysis — bioinformatics runs on Alliance Canada HPC
4. Results review — explore and interpret the pipeline outputs together
5. Figure workshop — create publication-quality figures
6. Manuscript drafting — write the paper together section by section
7. Peer review — AI reviewers check the manuscript

Right now you're helping with step {current_step}. Orient the researcher on where they
are and what comes next.

CONVERSATION APPROACH:
Start by briefly explaining your role, then get to know the researcher:
- What's their background? (career stage, field, institution)
- How were they involved in this project?
- What got them interested in this system or question?

Then naturally transition to the science:
- What's the research question or hypothesis?
- Why does this study matter?
- What do they expect to find, and what might surprise them?

SAMPLE SELECTION:
If samples haven't been submitted to the pipeline yet, help the researcher decide
which runs to include. They might say things like "let's analyze just the hot springs
samples above 90°C" or "include everything from site A but skip site B."

Use the FULL SAMPLE RECORDS below to identify which runs match their criteria.
When you and the researcher agree on a selection, update it by including this tag
in your response (it will be automatically processed):

[SELECT_RUNS: SRR123, SRR456, SRR789]

List the actual run accessions. The submission page will update to reflect the new
selection. The researcher can also manually adjust using checkboxes on the submission page.

{selected_runs_context}

SAMPLE DETAIL LOOKUP:
You have a browse_samples tool that lets you page through all per-sample metadata.
It returns shared columns (identical across all records) and varying columns per record.
Use it when you need specific details — run accessions, read counts, barcodes, etc.
You can search by keyword and page through results (50 per page).
Summarize what you find for the researcher — they don't need to see raw JSON.

Guidelines:
- One question at a time. Let the conversation breathe.
- Reference specific details from the metadata (locations, dates, organisms, temperatures).
- Follow up on interesting threads.
- Don't rush. The manuscript will be better for a thorough conversation.
- When you feel you have a good understanding, offer to summarize and suggest next steps.

PROJECT METADATA:
{metadata}
{sample_summary}
""",

    "results_review": """You are the OMC Research Assistant helping an author review their
pipeline results. The data is mounted at /data inside the container.

You can READ files to inspect results. For tabular files (.tsv, .csv), tell the author
what you find — column names, row counts, key statistics. For each result category,
explain what the analysis produced and what it means.

Your job is to PRESENT findings proactively:
- Summarize key results (MAG quality, taxonomy, community composition)
- Read and describe specific output files (e.g., CheckM2 quality, GTDB taxonomy)
- Highlight interesting or unexpected patterns
- Ask the author if results match their expectations
- Suggest which findings deserve emphasis in the manuscript
- The author can also explore data interactively in the Data Explorer tab

Keep guiding — don't wait for the author to ask what to do next.

INTERVIEW CONTEXT:
{interview_summary}
{sample_summary}

AVAILABLE DATA FILES:
{data_files}
""",

    "figure_workshop": """You are the OMC Research Assistant helping create figures for the
manuscript. You can generate Plotly visualizations.

Proactively suggest figures based on the results:
- Community composition bar charts
- MAG quality scatter plots (completeness vs contamination)
- Taxonomy trees / sunburst charts
- Diversity metrics across samples

For each figure, explain what it shows and why it matters. Ask the author for feedback
and iterate. When they approve, save the figure for the manuscript.

Generate Plotly JSON when creating figures. The author can view them in the data explorer.

RESULTS CONTEXT:
{results_summary}
""",

    "manuscript": """You are the OMC Research Assistant helping draft the manuscript.
Use the interview context, results, and approved figures to generate manuscript sections.

Work through sections in order: Abstract, Introduction, Methods, Results, Discussion.
For each section:
1. Generate a draft
2. Present it to the author
3. Incorporate feedback
4. Move to the next section

Write in scientific style appropriate for microbial ecology. Cite references as [CITE:topic].

FULL CONTEXT:
{full_context}
""",
}


def get_llm_client():
    return OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)


async def update_run_selection(accessions: list[str]) -> dict:
    """Update the selected runs on the portal via the LLM proxy endpoint."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{LLM_BASE_URL}/update-selection",
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                json={"run_accessions": accessions},
            )
            return resp.json()
    except Exception as e:
        return {"error": str(e)}


def list_data_files():
    """List available data files with descriptions of what they contain."""
    if not DATA_DIR.exists():
        return "No data directory mounted."

    # Known pipeline output directories and what they contain
    DIR_DESCRIPTIONS = {
        "assembly": "Genome assembly results (contigs, scaffolds, assembly stats)",
        "binning": "MAG binning results (bins, quality reports from CheckM2/DAS Tool/MAGSCOT)",
        "taxonomy": "Taxonomic classification (GTDB-Tk, Kaiju, Kraken2 reports)",
        "annotation": "Gene annotation (Bakta, Prokka, functional annotations)",
        "metabolism": "Metabolic pathway analysis (DRAM, KEGG, COG assignments)",
        "mge": "Mobile genetic elements (CARD, Genomad, DefenseFinder, IslandPath)",
        "eukaryotic": "Eukaryotic content analysis",
        "viz": "Visualization outputs",
        "mapping": "Read mapping statistics (coverage, depth per contig/bin)",
        "pipeline_info": "Nextflow pipeline execution logs and reports",
    }

    # File type hints
    FILE_HINTS = {
        ".tsv": "tab-separated table — read with pandas: pd.read_csv(path, sep='\\t')",
        ".csv": "comma-separated table — read with pandas: pd.read_csv(path)",
        ".txt": "text file — may be tabular or freeform",
        ".json": "JSON data — read with json.load(open(path))",
        ".html": "HTML report — can be viewed in browser",
        ".fasta": "FASTA sequences",
        ".fa": "FASTA sequences",
        ".fna": "FASTA nucleotide sequences",
        ".faa": "FASTA protein sequences",
        ".gff": "GFF3 gene annotations",
        ".gff3": "GFF3 gene annotations",
        ".gbk": "GenBank format annotations",
        ".nwk": "Newick tree format",
        ".log": "Log file",
    }

    output = []

    # List directories with descriptions
    for subdir in sorted(DATA_DIR.iterdir()):
        if not subdir.is_dir() or subdir.name.startswith("."):
            continue
        desc = DIR_DESCRIPTIONS.get(subdir.name, "")
        files = sorted(f for f in subdir.rglob("*") if f.is_file() and not f.name.startswith("."))
        if not files:
            continue

        output.append(f"\n📁 {subdir.name}/ ({len(files)} files){' — ' + desc if desc else ''}")
        for f in files[:20]:
            size = f.stat().st_size
            size_str = f"{size / 1024:.0f}K" if size < 1_000_000 else f"{size / 1_000_000:.1f}M"
            rel = f.relative_to(DATA_DIR)
            hint = FILE_HINTS.get(f.suffix.lower(), "")
            # Preview first line of small TSV/CSV files
            preview = ""
            if f.suffix.lower() in (".tsv", ".csv") and size < 10_000_000:
                try:
                    first_line = f.read_text().split("\n")[0][:200]
                    preview = f"  columns: {first_line}"
                except Exception:
                    pass
            output.append(f"  {rel} ({size_str}){' — ' + hint if hint else ''}")
            if preview:
                output.append(preview)
        if len(files) > 20:
            output.append(f"  ... and {len(files) - 20} more files")

    # Top-level files
    top_files = sorted(f for f in DATA_DIR.iterdir() if f.is_file() and not f.name.startswith("."))
    if top_files:
        output.append("\n📄 Top-level files:")
        for f in top_files:
            size = f.stat().st_size
            size_str = f"{size / 1024:.0f}K" if size < 1_000_000 else f"{size / 1_000_000:.1f}M"
            output.append(f"  {f.name} ({size_str})")

    return "\n".join(output) if output else "No data files found."


def get_sample_summary():
    """Summarize sample metadata from metadata.json for the AI."""
    try:
        metadata = json.loads(SUBMISSION_META)
    except (json.JSONDecodeError, TypeError):
        return ""

    sample_meta = metadata.get("sample_metadata", {})
    records = sample_meta.get("sample_records", [])
    if not records:
        return ""

    lines = [f"\nSAMPLE METADATA ({len(records)} runs):"]

    # Summarize unique values for key fields
    key_fields = ["collection_date", "country", "host", "scientific_name",
                  "environment_biome", "environment_feature", "environment_material",
                  "instrument_platform", "library_strategy", "depth", "altitude"]
    for field in key_fields:
        values = set(r.get(field, "") for r in records if r.get(field))
        if values:
            label = field.replace("_", " ").title()
            if len(values) <= 5:
                lines.append(f"  {label}: {', '.join(sorted(values))}")
            else:
                lines.append(f"  {label}: {len(values)} unique values")

    # Show first record as example
    if records:
        lines.append(f"\n  Example record (run {records[0].get('run_accession', '?')}):")
        for k, v in sorted(records[0].items()):
            if v and k not in ("study_accession", "study_title", "experiment_accession",
                               "sample_description", "description", "center_name"):
                lines.append(f"    {k}: {v}")

    return "\n".join(lines)


def get_status_context():
    """Describe where we are in the workflow based on available data."""
    try:
        metadata = json.loads(SUBMISSION_META)
    except (json.JSONDecodeError, TypeError):
        return "No project data loaded yet."

    has_results = any(DATA_DIR.iterdir()) if DATA_DIR.exists() else False
    has_pipeline_dirs = any(
        (DATA_DIR / d).exists()
        for d in ["assembly", "binning", "taxonomy", "annotation", "metabolism"]
    ) if DATA_DIR.exists() else False
    selected = metadata.get("interview_data", {}).get("_selected_run_accessions", [])
    all_records = metadata.get("sample_metadata", {}).get("sample_records", [])

    if has_pipeline_dirs:
        return ("Pipeline results are available. The bioinformatics analysis has completed "
                "and the results are ready for review.")
    elif selected:
        return (f"{len(selected)} runs have been selected for analysis. "
                "The pipeline may be running or queued on Alliance Canada HPC.")
    elif all_records:
        return (f"The project has {len(all_records)} sequencing runs available. "
                "No runs have been submitted to the pipeline yet — help the researcher "
                "decide which samples to analyze.")
    else:
        return "Project metadata has been loaded but no sample details are available yet."


def get_selected_runs_context():
    """Describe current sample selection state."""
    try:
        metadata = json.loads(SUBMISSION_META)
    except (json.JSONDecodeError, TypeError):
        return ""

    selected_accs = set(metadata.get("interview_data", {}).get("_selected_run_accessions", []))
    records = metadata.get("sample_metadata", {}).get("sample_records", [])
    if not records:
        return ""

    if not selected_accs:
        return f"\nSELECTED RUNS: None yet — all {len(records)} runs are available for selection."

    selected_records = [r for r in records if r.get("run_accession") in selected_accs]
    unselected = [r for r in records if r.get("run_accession") not in selected_accs]

    MAX_SHOW = 10  # Cap listed runs to keep prompt small

    lines = [f"\nSELECTED RUNS ({len(selected_records)} of {len(records)}):"]
    for r in selected_records[:MAX_SHOW]:
        lines.append(f"  ✓ {r.get('run_accession', '?')} — {r.get('sample_alias', '')} "
                     f"({r.get('country', '')} {r.get('collection_date', '')})")
    if len(selected_records) > MAX_SHOW:
        lines.append(f"  ... and {len(selected_records) - MAX_SHOW} more selected runs")

    if unselected:
        lines.append(f"\nNOT SELECTED ({len(unselected)} runs)")
        for r in unselected[:MAX_SHOW]:
            lines.append(f"  ✗ {r.get('run_accession', '?')} — {r.get('sample_alias', '')} "
                         f"({r.get('country', '')} {r.get('collection_date', '')})")
        if len(unselected) > MAX_SHOW:
            lines.append(f"  ... and {len(unselected) - MAX_SHOW} more")

    return "\n".join(lines)


def get_sample_records_json():
    """Get sample records as compact JSON, trimmed to fit context window.

    Keeps only fields useful for sample selection and interview context.
    Caps total output to ~30K chars to stay well within LLM context limits.
    """
    try:
        metadata = json.loads(SUBMISSION_META)
    except (json.JSONDecodeError, TypeError):
        return "[]"
    records = metadata.get("sample_metadata", {}).get("sample_records", [])
    if not records:
        return "[]"

    # Fields the AI needs for sample selection and interview
    KEEP_FIELDS = {
        "run_accession", "sample_accession", "sample_alias", "sample_title",
        "scientific_name", "organism", "host", "collection_date", "country",
        "geo_loc_name", "lat_lon", "environment_biome", "environment_feature",
        "environment_material", "depth", "altitude", "temperature",
        "instrument_platform", "instrument_model", "library_strategy",
        "library_source", "library_layout", "read_count", "base_count",
    }

    trimmed = []
    for r in records:
        entry = {k: v for k, v in r.items() if k in KEEP_FIELDS and v}
        trimmed.append(entry)

    result = json.dumps(trimmed, indent=1, default=str)

    # Hard cap — if still too large, truncate to first N records
    MAX_CHARS = 30_000
    if len(result) > MAX_CHARS:
        for limit in range(len(trimmed), 0, -1):
            result = json.dumps(trimmed[:limit], indent=1, default=str)
            if len(result) <= MAX_CHARS:
                result += f"\n(showing {limit} of {len(records)} records)"
                break

    return result


def get_current_step():
    """Determine which workflow step we're on."""
    has_pipeline_dirs = any(
        (DATA_DIR / d).exists()
        for d in ["assembly", "binning", "taxonomy", "annotation", "metabolism"]
    ) if DATA_DIR.exists() else False
    if has_pipeline_dirs:
        return "4 (Results Review)"
    return "1-2 (Data Selection & Interview)"


def build_interview_system():
    """Build the full interview system prompt with all context.

    Only includes bioproject-level metadata and a sample summary in the prompt.
    Full per-sample records are available on-demand via get_sample_records_json().
    """
    try:
        metadata = json.loads(SUBMISSION_META)
    except (json.JSONDecodeError, TypeError):
        metadata = {}

    # Strip bulky fields — sample records, run lists, and large breakdowns
    SKIP_KEYS = {"sample_metadata", "selected_runs"}
    meta_lite = {k: v for k, v in metadata.items() if k not in SKIP_KEYS}

    # Keep sample count but not full records
    sample_meta = metadata.get("sample_metadata", {})
    if sample_meta:
        meta_lite["sample_count"] = len(sample_meta.get("sample_records", []))

    # Strip selected_run_accessions from interview_data (shown separately via selected_runs_context)
    if "interview_data" in meta_lite and isinstance(meta_lite["interview_data"], dict):
        meta_lite["interview_data"] = {
            k: v for k, v in meta_lite["interview_data"].items()
            if k != "_selected_run_accessions"
        }

    # Cap the metadata dump to avoid blowing up the prompt
    meta_json = json.dumps(meta_lite, indent=2, default=str)
    if len(meta_json) > 10_000:
        # Keep only the most useful top-level fields
        PRIORITY_KEYS = {"slug", "accession", "pipeline", "title", "sample_count",
                         "interview_data", "organism", "description"}
        meta_lite = {k: v for k, v in meta_lite.items() if k in PRIORITY_KEYS}
        meta_json = json.dumps(meta_lite, indent=2, default=str)

    return SYSTEM_PROMPTS["interview"].format(
        metadata=meta_json,
        sample_summary=get_sample_summary(),
        status_context=get_status_context(),
        selected_runs_context=get_selected_runs_context(),
        current_step=get_current_step(),
    )


# ── Session lifecycle ────────────────────────────────────────────────────────

@cl.on_chat_start
async def on_start():
    """Initialize session: set phase, load metadata, greet the author."""
    # Parse submission metadata
    try:
        metadata = json.loads(SUBMISSION_META)
    except json.JSONDecodeError:
        metadata = {}

    cl.user_session.set("metadata", metadata)

    # Fresh session — the data layer + on_chat_resume handles thread persistence
    cl.user_session.set("phase", "interview")
    cl.user_session.set("interview_summary", "")
    cl.user_session.set("results_summary", "")
    cl.user_session.set("history", [])

    # Generate opening message
    client = get_llm_client()
    system = build_interview_system()

    opening_prompt = (
        "The researcher just opened their session. "
        "Greet them warmly and naturally. Mention something specific you noticed "
        "in their project metadata (a detail about the sampling site, organism, "
        "or study design — something that shows you've looked at their data). "
        "Then ask them to tell you a bit about themselves — what's their role, "
        "how did they get involved in this project? Keep it conversational and "
        "under 120 words."
    )

    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": opening_prompt},
            ],
            max_tokens=1000,
            temperature=0.7,
        )
        greeting = (resp.choices[0].message.content
                    if resp.choices and resp.choices[0].message
                    else None)
        if not greeting:
            greeting = "Welcome! I'm the OMC Research Assistant. Tell me about your research."
    except Exception as e:
        greeting = (
            f"Welcome to the OMC Research Assistant! I'll help guide you through "
            f"reviewing your results and preparing your manuscript.\n\n"
            f"Let's start with the basics — what's the main research question "
            f"you're investigating with this dataset?\n\n"
            f"*(LLM connection issue: {e})*"
        )

    now = datetime.now(timezone.utc).isoformat()
    cl.user_session.set("history", [
        {"role": "assistant", "content": greeting, "timestamp": now}
    ])

    _save_state()
    await cl.Message(content=greeting).send()


@cl.on_chat_resume
async def on_chat_resume(thread: dict):
    """Resume a thread from the sidebar — restore session state."""
    try:
        metadata = json.loads(SUBMISSION_META)
    except json.JSONDecodeError:
        metadata = {}

    cl.user_session.set("metadata", metadata)

    # Rebuild history from persisted steps
    history = []
    for step in thread.get("steps", []):
        step_type = step.get("type", "")
        output = step.get("output", "")
        if not output:
            continue
        if step_type == "user_message":
            history.append({"role": "user", "content": output})
        elif step_type == "assistant_message":
            history.append({"role": "assistant", "content": output})

    # Restore phase and summaries from thread metadata
    thread_meta = thread.get("metadata", {}) or {}
    cl.user_session.set("phase", thread_meta.get("phase", "interview"))
    cl.user_session.set("interview_summary", thread_meta.get("interview_summary", ""))
    cl.user_session.set("results_summary", thread_meta.get("results_summary", ""))
    cl.user_session.set("history", history)


async def _llm_with_tools(client, messages: list, tools: list, msg: cl.Message, max_rounds: int = 5) -> str:
    """Call the LLM, handle tool calls in a loop, stream the final text response."""

    for _ in range(max_rounds):
        # Non-streaming call to check for tool use
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            tools=tools,
            max_tokens=10000,
            temperature=0.7,
        )

        choice = resp.choices[0] if resp.choices else None
        if not choice:
            return "No response from the model."

        # If the model wants to call tools
        if choice.finish_reason == "tool_calls" or (choice.message and choice.message.tool_calls):
            tool_calls = choice.message.tool_calls or []
            # Add the assistant message with tool calls to context
            messages.append(choice.message)

            # Show a brief status to the user
            tool_names = [tc.function.name for tc in tool_calls]
            await msg.stream_token(f"*Looking up {', '.join(tool_names)}...*\n\n")

            # Execute each tool call
            for tc in tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                result = await execute_tool(tc.function.name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

            # Continue the loop — the LLM will see the tool results
            continue

        # No tool calls — stream the text response
        # We got a non-streaming response, just use its content
        text = choice.message.content if choice.message else ""
        if text:
            await msg.stream_token(text)
        await msg.update()
        return text or ""

    # Exhausted rounds
    await msg.update()
    return msg.content or ""


@cl.on_message
async def on_message(message: cl.Message):
    """Handle each author message — route to current phase."""
    phase = cl.user_session.get("phase")
    metadata = cl.user_session.get("metadata", {})
    history = cl.user_session.get("history", [])

    # Add user message to history
    now = datetime.now(timezone.utc).isoformat()
    history.append({"role": "user", "content": message.content, "timestamp": now})

    # Build system prompt for current phase
    if phase == "interview":
        system = build_interview_system()
    elif phase == "results_review":
        system = SYSTEM_PROMPTS["results_review"].format(
            interview_summary=cl.user_session.get("interview_summary", ""),
            sample_summary=get_sample_summary(),
            data_files=list_data_files(),
        )
    elif phase == "figure_workshop":
        system = SYSTEM_PROMPTS["figure_workshop"].format(
            results_summary=cl.user_session.get("results_summary", ""),
        )
    elif phase == "manuscript":
        system = SYSTEM_PROMPTS["manuscript"].format(
            full_context=json.dumps({
                "metadata": metadata,
                "interview": cl.user_session.get("interview_summary", ""),
                "results": cl.user_session.get("results_summary", ""),
            }, indent=2, default=str),
        )
    else:
        system = "You are a helpful research assistant."

    # Call LLM with tool support
    client = get_llm_client()
    llm_messages = [{"role": "system", "content": system}] + history[-20:]
    tools = get_tool_schemas()

    msg = cl.Message(content="")
    await msg.send()

    try:
        full_response = await _llm_with_tools(client, llm_messages, tools, msg)
    except Exception as e:
        full_response = f"I'm having trouble connecting to the AI. Error: {e}"
        msg.content = full_response
        await msg.update()

    # Save to history
    now = datetime.now(timezone.utc).isoformat()
    history.append({"role": "assistant", "content": full_response, "timestamp": now})
    cl.user_session.set("history", history)

    # Persist state to container-local file
    _save_state()

    # Check for run selection updates
    await _check_run_selection(full_response)

    # Check for phase transitions
    await _check_phase_transition(full_response)


async def _check_run_selection(response: str):
    """Detect and execute run selection updates from the AI response.

    The AI uses [SELECT_RUNS: SRR123, SRR456, ...] to update the selection.
    """
    import re
    match = re.search(r'\[SELECT_RUNS:\s*(.*?)\]', response, re.IGNORECASE)
    if not match:
        return

    accessions_str = match.group(1).strip()
    accessions = [a.strip() for a in accessions_str.split(",") if a.strip()]
    if not accessions:
        return

    result = await update_run_selection(accessions)
    if result.get("ok"):
        await cl.Message(
            content=f"*Updated selection: {len(accessions)} runs selected. "
                    f"The submission page will reflect this change.*",
        ).send()
    elif result.get("error"):
        await cl.Message(
            content=f"*Could not update selection: {result['error']}*",
        ).send()


async def _check_phase_transition(response: str):
    """Detect when the AI signals a phase transition."""
    phase = cl.user_session.get("phase")
    lower = response.lower()

    if phase == "interview" and any(phrase in lower for phrase in [
        "ready to move to results",
        "let's look at your results",
        "move on to reviewing",
        "ready to review your data",
        "let me summarize what i've learned",
    ]):
        # Extract interview summary for next phase
        cl.user_session.set("interview_summary", response)
        cl.user_session.set("phase", "results_review")
        await cl.Message(
            content="---\n**Phase: Results Review** — I'll now walk you through your pipeline results.\n\n"
            f"You can also explore your data interactively: [Open Data Explorer]({MARIMO_URL})",
        ).send()

    elif phase == "results_review" and any(phrase in lower for phrase in [
        "ready for figures",
        "let's create some figures",
        "move to figures",
        "start on the figures",
    ]):
        cl.user_session.set("results_summary", response)
        cl.user_session.set("phase", "figure_workshop")
        await cl.Message(
            content="---\n**Phase: Figure Workshop** — Let's create the figures for your manuscript.",
        ).send()

    elif phase == "figure_workshop" and any(phrase in lower for phrase in [
        "ready for the manuscript",
        "start drafting",
        "move to manuscript",
        "begin writing",
    ]):
        cl.user_session.set("phase", "manuscript")
        await cl.Message(
            content="---\n**Phase: Manuscript Drafting** — Let's write your paper.",
        ).send()
