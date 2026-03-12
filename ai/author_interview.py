"""AI-powered author interview for gathering manuscript context."""
from anthropic import Anthropic
from typing import Optional


async def conduct_interview_turn(
    api_key: str,
    question: str,
    answer: str,
    previous_answers: dict,
    sra_accession: str,
    pipeline_type: str,
) -> dict:
    """
    Process an interview answer and provide follow-up if needed.

    The interview is mostly structured (predefined questions), but this
    can provide intelligent follow-up questions or clarifications.
    """
    client = Anthropic(api_key=api_key)

    context = f"""SRA Accession: {sra_accession}
Pipeline: {pipeline_type}

Previous answers:
{_format_previous(previous_answers)}

Current question: {question}
Author's answer: {answer}
"""

    prompt = f"""Review this interview response from a researcher submitting a paper to OMC:

{context}

Evaluate the response:
1. Is it sufficiently detailed for manuscript generation?
2. Are there any clarifications needed?
3. Any follow-up questions that would help?

If the answer is good, respond with: {{"status": "accepted", "feedback": null}}
If clarification needed, respond with: {{"status": "needs_clarification", "feedback": "Your follow-up question or prompt"}}

Only ask for clarification if truly needed - don't be unnecessarily demanding."""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = response.content[0].text

    # Parse response
    import json
    try:
        # Try to extract JSON from response
        if "{" in response_text:
            json_start = response_text.index("{")
            json_end = response_text.rindex("}") + 1
            result = json.loads(response_text[json_start:json_end])
            return result
    except (ValueError, json.JSONDecodeError):
        pass

    # Default to accepting
    return {"status": "accepted", "feedback": None}


async def generate_interview_summary(
    api_key: str,
    interview_data: dict,
    sra_accession: str,
    pipeline_type: str,
) -> str:
    """Generate a summary of the interview for the manuscript generation step."""
    client = Anthropic(api_key=api_key)

    prompt = f"""Summarize this author interview for use in manuscript generation:

SRA Accession: {sra_accession}
Pipeline: {pipeline_type}

Interview Responses:
{_format_previous(interview_data)}

Create a concise summary (2-3 paragraphs) that captures:
1. The research question and context
2. Key sample/study information
3. Expected findings and significance
4. Any limitations or caveats

This summary will be used by the AI to generate appropriate manuscript sections."""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
    )

    return response.content[0].text


def _format_previous(answers: dict) -> str:
    """Format previous answers for context."""
    if not answers:
        return "None yet"

    formatted = []
    for key, value in answers.items():
        label = key.replace("_", " ").title()
        formatted.append(f"{label}:\n{value}")

    return "\n\n".join(formatted)
