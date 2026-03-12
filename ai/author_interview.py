"""AI-powered author interview for gathering manuscript context."""
import json
from .llm_client import get_client, chat, multi_turn


INTERVIEW_SYSTEM = """You are a scientific editor conducting an author interview for the
Open Microbial Community (OMC) platform. Your job is to gather enough context from the
researcher to generate a good first draft of their manuscript.

You have access to the project's SRA metadata (provided below). Use it to ask informed
questions — don't ask the author things you can already see in the metadata.

Be conversational but efficient. Each message should either:
1. Ask a focused question about the research
2. Confirm your understanding of something
3. Summarize what you've learned so far

Topics to cover (not necessarily in order):
- Research question / hypothesis
- Why this study matters (broader significance)
- Key references the author wants cited
- Expected vs surprising findings
- Known limitations or caveats
- Sample selection rationale (which runs to include and why)
- Any methodological choices the author wants highlighted

When you have enough context, say so and provide a structured summary.
Do NOT ask more than 8-10 questions total. Respect the author's time.
"""


async def conduct_interview_turn(
    message: str,
    conversation_history: list[dict],
    project_metadata: dict,
    pipeline_type: str,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> dict:
    """
    Process one turn of the interview conversation.

    Args:
        message: The author's latest message
        conversation_history: List of {"role": ..., "content": ...} dicts
        project_metadata: The BioProject metadata dict
        pipeline_type: Selected pipeline

    Returns:
        {"response": str, "complete": bool, "summary": dict|None}
    """
    client = get_client(base_url=base_url, api_key=api_key)

    # Build system prompt with project context
    meta_context = json.dumps({
        k: v for k, v in project_metadata.items()
        if k not in ("breakdown",)  # skip the big breakdown table
    }, indent=2, default=str) if project_metadata else "Not available"

    system = f"""{INTERVIEW_SYSTEM}

PROJECT METADATA:
{meta_context}

PIPELINE: {pipeline_type}
"""

    # Add the author's new message to history
    messages = list(conversation_history) + [{"role": "user", "content": message}]

    response_text = multi_turn(
        client, system, messages,
        model=model, max_tokens=1000, temperature=0.7,
    )

    # Check if the interview is complete (AI says it has enough)
    complete = any(phrase in response_text.lower() for phrase in [
        "i have enough", "that covers everything", "ready to generate",
        "sufficient context", "let me summarize what i've learned",
        "here's a summary of what i've gathered",
    ])

    result = {
        "response": response_text,
        "complete": complete,
        "summary": None,
    }

    # If complete, extract the structured summary
    if complete:
        result["summary"] = await _extract_summary(
            client, messages + [{"role": "assistant", "content": response_text}],
            model=model,
        )

    return result


async def start_interview(
    project_metadata: dict,
    pipeline_type: str,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> str:
    """Generate the opening message for the interview."""
    client = get_client(base_url=base_url, api_key=api_key)

    meta_context = json.dumps({
        k: v for k, v in project_metadata.items()
        if k not in ("breakdown",)
    }, indent=2, default=str) if project_metadata else "Not available"

    return chat(client,
        f"""{INTERVIEW_SYSTEM}

PROJECT METADATA:
{meta_context}

PIPELINE: {pipeline_type}
""",
        """This is the start of the interview. The author just submitted their BioProject
and selected a pipeline. Introduce yourself briefly, show that you've reviewed their
metadata (mention something specific you noticed), and ask your first question.
Keep it under 150 words.""",
        model=model, max_tokens=300, temperature=0.7,
    )


async def _extract_summary(
    client,
    conversation: list[dict],
    model: str | None = None,
) -> dict:
    """Extract a structured summary from the completed interview."""
    summary_prompt = """Based on the interview conversation above, extract a structured summary as JSON:

{
    "research_question": "...",
    "study_context": "...",
    "sample_info": "...",
    "expected_findings": "...",
    "broader_significance": "...",
    "limitations": "...",
    "key_references": ["...", "..."],
    "notes": "Any other relevant context"
}

Return ONLY the JSON."""

    messages = list(conversation) + [{"role": "user", "content": summary_prompt}]

    response = multi_turn(
        client,
        "Extract structured information from the interview. Return only valid JSON.",
        messages,
        model=model, max_tokens=1000, temperature=0.3,
    )

    try:
        text = response.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        return json.loads(text)
    except (ValueError, json.JSONDecodeError):
        return {"raw_summary": response}
