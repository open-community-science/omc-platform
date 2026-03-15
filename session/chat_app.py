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
SUBMISSION_META = os.environ.get("SUBMISSION_META", "{}")

# ── Phases ───────────────────────────────────────────────────────────────────

PHASES = ["interview", "results_review", "figure_workshop", "manuscript"]

SYSTEM_PROMPTS = {
    "interview": """You are the OMC Research Assistant, conducting an author interview for a
scientific manuscript on microbial ecology / metagenomics.

You have the project metadata below. Your job is to PULL the author through the interview —
ask focused questions one at a time, show that you've reviewed their data, and keep things
moving. Do NOT wait passively. Each message should end with a clear question or next step.

Topics to cover (adapt order to the conversation):
- Research question / hypothesis
- Why this study matters
- Sample selection rationale
- Expected vs surprising findings
- Key references to cite
- Known limitations

When you have enough context (6-8 questions max), summarize what you learned and tell
the author you're ready to move to results review.

PROJECT METADATA:
{metadata}
""",

    "results_review": """You are the OMC Research Assistant helping an author review their
pipeline results. You have access to the pipeline output files in /data.

Your job is to PRESENT findings proactively:
- Summarize key results (MAG quality, taxonomy, community composition)
- Highlight interesting or unexpected patterns
- Ask the author if results match their expectations
- Suggest which findings deserve emphasis in the manuscript
- When appropriate, offer to open the interactive data explorer

Keep guiding — don't wait for the author to ask what to do next.

INTERVIEW CONTEXT:
{interview_summary}

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
    """List available data files in the mounted data directory."""
    if not DATA_DIR.exists():
        return "No data directory mounted."
    files = []
    for p in sorted(DATA_DIR.rglob("*")):
        if p.is_file() and not p.name.startswith("."):
            size = p.stat().st_size
            size_str = f"{size / 1024:.0f}K" if size < 1_000_000 else f"{size / 1_000_000:.1f}M"
            files.append(f"  {p.relative_to(DATA_DIR)} ({size_str})")
    return "\n".join(files[:50]) if files else "No data files found."


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
        metadata=json.dumps(metadata, indent=2, default=str)
    )

    opening_prompt = (
        "The author just opened their session. Their pipeline results are ready. "
        "Introduce yourself warmly, mention something specific from their metadata, "
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
    if phase == "interview":
        system = SYSTEM_PROMPTS["interview"].format(
            metadata=json.dumps(metadata, indent=2, default=str)
        )
    elif phase == "results_review":
        system = SYSTEM_PROMPTS["results_review"].format(
            interview_summary=cl.user_session.get("interview_summary", ""),
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
