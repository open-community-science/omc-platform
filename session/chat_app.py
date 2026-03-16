"""OMC Research Assistant — Chainlit chat app for author sessions.

Drives the author through interview → results review → figure workshop → manuscript.
Connects to an OpenAI-compatible LLM endpoint.
"""

import os
import json
from pathlib import Path

import chainlit as cl
from openai import OpenAI

# ── Config from environment (set by portal when launching container) ─────────

# When launched by the portal, LLM_BASE_URL points to the portal's LLM proxy
# (e.g., http://172.30.0.1:8002/api/llm) and LLM_API_KEY is a session token.
# The OpenAI SDK appends /chat/completions automatically.
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://host.docker.internal:8002/api/llm")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "not-needed")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen3-coder-30b-a3b-instruct")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
MARIMO_URL = os.environ.get("MARIMO_URL", "http://localhost:8081")

# Load metadata from file (round-trips with the data through the pipeline)
# Falls back to SUBMISSION_META env var for backwards compat
_meta_file = DATA_DIR / "metadata.json"
if _meta_file.exists():
    SUBMISSION_META = _meta_file.read_text()
else:
    SUBMISSION_META = os.environ.get("SUBMISSION_META", "{}")

# ── Phases ───────────────────────────────────────────────────────────────────

PHASES = ["interview", "results_review", "figure_workshop", "manuscript"]

SYSTEM_PROMPTS = {
    "interview": """You are the OMC Research Assistant, conducting an author interview for a
scientific manuscript on microbial ecology / metagenomics.

You have the project metadata and per-sample metadata below. Your job is to PULL the author
through the interview — ask focused questions one at a time, show that you've reviewed their
data (mention specific details you see in the metadata), and keep things moving.
Do NOT wait passively. Each message should end with a clear question or next step.

Topics to cover (adapt order to the conversation):
- Research question / hypothesis
- Why this study matters
- Sample selection rationale (reference the actual samples, dates, locations you see)
- Expected vs surprising findings
- Key references to cite
- Known limitations

When you have enough context (6-8 questions max), summarize what you learned and tell
the author you're ready to move to results review.

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


# ── Session lifecycle ────────────────────────────────────────────────────────

@cl.on_chat_start
async def on_start():
    """Initialize session: set phase, load metadata, greet the author."""
    # Parse submission metadata
    try:
        metadata = json.loads(SUBMISSION_META)
    except json.JSONDecodeError:
        metadata = {}

    # Session state
    cl.user_session.set("phase", "interview")
    cl.user_session.set("metadata", metadata)
    cl.user_session.set("interview_summary", "")
    cl.user_session.set("results_summary", "")
    cl.user_session.set("history", [])

    # Generate opening message
    client = get_llm_client()
    system = SYSTEM_PROMPTS["interview"].format(
        metadata=json.dumps(metadata, indent=2, default=str),
        sample_summary=get_sample_summary(),
    )

    opening_prompt = (
        "The author just opened their session. "
        "Introduce yourself warmly, mention something specific from their metadata "
        "(like the sampling location, date, organism, or number of samples), "
        "and ask your first interview question. Keep it under 150 words."
    )

    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": opening_prompt},
            ],
            max_tokens=300,
            temperature=0.7,
        )
        greeting = resp.choices[0].message.content
    except Exception as e:
        greeting = (
            f"Welcome to the OMC Research Assistant! I'll help guide you through "
            f"reviewing your results and preparing your manuscript.\n\n"
            f"Let's start with the basics — what's the main research question "
            f"you're investigating with this dataset?\n\n"
            f"*(LLM connection issue: {e})*"
        )

    cl.user_session.set("history", [
        {"role": "assistant", "content": greeting}
    ])

    await cl.Message(content=greeting).send()


@cl.on_message
async def on_message(message: cl.Message):
    """Handle each author message — route to current phase."""
    phase = cl.user_session.get("phase")
    metadata = cl.user_session.get("metadata", {})
    history = cl.user_session.get("history", [])

    # Add user message to history
    history.append({"role": "user", "content": message.content})

    # Build system prompt for current phase
    sample_summary = get_sample_summary()

    if phase == "interview":
        system = SYSTEM_PROMPTS["interview"].format(
            metadata=json.dumps(metadata, indent=2, default=str),
            sample_summary=sample_summary,
        )
    elif phase == "results_review":
        system = SYSTEM_PROMPTS["results_review"].format(
            interview_summary=cl.user_session.get("interview_summary", ""),
            sample_summary=sample_summary,
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

    # Call LLM
    client = get_llm_client()
    messages = [{"role": "system", "content": system}] + history[-20:]  # keep last 20 turns

    msg = cl.Message(content="")
    await msg.send()

    try:
        stream = client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            max_tokens=1000,
            temperature=0.7,
            stream=True,
        )
        full_response = ""
        for chunk in stream:
            delta = chunk.choices[0].delta.content or ""
            full_response += delta
            await msg.stream_token(delta)

        await msg.update()
    except Exception as e:
        full_response = f"I'm having trouble connecting to the AI. Error: {e}"
        msg.content = full_response
        await msg.update()

    # Save to history
    history.append({"role": "assistant", "content": full_response})
    cl.user_session.set("history", history)

    # Check for phase transitions
    await _check_phase_transition(full_response)


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
