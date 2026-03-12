"""AI-powered manuscript generation from pipeline outputs and author interview."""
from anthropic import Anthropic
from typing import Optional
import yaml
import json


async def generate_manuscript_draft(
    api_key: str,
    pipeline_outputs: dict,
    interview_data: dict,
    pipeline_type: str,
    sra_accession: str,
) -> dict:
    """
    Generate a manuscript draft from pipeline outputs and author interview.

    Returns a dict with sections: introduction, methods, results, discussion
    """
    client = Anthropic(api_key=api_key)

    # Build context from pipeline outputs
    results_summary = _summarize_results(pipeline_outputs)

    # Build context from interview
    interview_context = _format_interview(interview_data)

    system_prompt = """You are a scientific writing assistant for microbial ecology research.
You help generate clear, accurate manuscript drafts based on bioinformatics pipeline outputs
and author-provided context. Write in a professional academic style suitable for publication.

Key principles:
- Be precise and accurate about the data
- Acknowledge limitations
- Use appropriate hedging language for interpretations
- Follow standard scientific paper structure
- Include placeholders [CITE] where citations would be needed
"""

    # Generate each section
    sections = {}

    # Introduction
    intro_prompt = f"""Generate an Introduction section for a paper with this context:

RESEARCH CONTEXT FROM AUTHOR:
{interview_context}

SRA Accession: {sra_accession}
Pipeline: {pipeline_type}

Write 2-3 paragraphs that:
1. Establish the scientific context and importance
2. State the research question/hypothesis
3. Briefly preview the approach

Use [CITE] placeholders where literature citations would go."""

    intro_response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        system=system_prompt,
        messages=[{"role": "user", "content": intro_prompt}],
    )
    sections["introduction"] = intro_response.content[0].text

    # Methods (largely templated from pipeline)
    methods_prompt = f"""Generate a Methods section for this analysis:

Pipeline: {pipeline_type}
SRA Accession: {sra_accession}

AUTHOR CONTEXT ON SAMPLES:
{interview_data.get('study_context', 'Not provided')}
{interview_data.get('sample_info', 'Not provided')}

PIPELINE OUTPUTS:
{results_summary}

Write a Methods section covering:
1. Data acquisition (SRA)
2. Sequence processing pipeline
3. Analysis parameters (inferred from outputs)
4. Statistical approaches used

Be specific about tools and versions where inferable."""

    methods_response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        system=system_prompt,
        messages=[{"role": "user", "content": methods_prompt}],
    )
    sections["methods"] = methods_response.content[0].text

    # Results
    results_prompt = f"""Generate a Results section based on these pipeline outputs:

{json.dumps(pipeline_outputs, indent=2, default=str)}

RESEARCH QUESTION:
{interview_data.get('research_question', 'Not specified')}

Write a Results section that:
1. Reports findings objectively without interpretation
2. References figures and tables (Figure 1, Table 1, etc.)
3. Includes key statistics and quantitative findings
4. Follows a logical flow from overview to specific findings

Do not interpret results - save that for Discussion."""

    results_response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=3000,
        system=system_prompt,
        messages=[{"role": "user", "content": results_prompt}],
    )
    sections["results"] = results_response.content[0].text

    # Discussion
    discussion_prompt = f"""Generate a Discussion section:

KEY RESULTS:
{sections['results'][:1500]}

AUTHOR EXPECTATIONS:
{interview_data.get('expected_findings', 'Not specified')}

BROADER SIGNIFICANCE (from author):
{interview_data.get('broader_significance', 'Not specified')}

KNOWN LIMITATIONS (from author):
{interview_data.get('limitations', 'Not specified')}

Write a Discussion that:
1. Summarizes main findings and their significance
2. Compares with expected findings and explains differences
3. Places results in broader context
4. Addresses limitations honestly
5. Suggests future directions

Use [CITE] placeholders for literature references."""

    discussion_response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=3000,
        system=system_prompt,
        messages=[{"role": "user", "content": discussion_prompt}],
    )
    sections["discussion"] = discussion_response.content[0].text

    return sections


def _summarize_results(pipeline_outputs: dict) -> str:
    """Create a text summary of pipeline outputs for the prompt."""
    if not pipeline_outputs:
        return "No pipeline outputs available yet."

    summary_parts = []
    for key, value in pipeline_outputs.items():
        if isinstance(value, dict):
            summary_parts.append(f"{key}: {json.dumps(value, indent=2, default=str)}")
        else:
            summary_parts.append(f"{key}: {value}")

    return "\n".join(summary_parts)


def _format_interview(interview_data: dict) -> str:
    """Format interview responses for the prompt."""
    if not interview_data:
        return "No author interview data available."

    formatted = []
    for key, value in interview_data.items():
        label = key.replace("_", " ").title()
        formatted.append(f"{label}: {value}")

    return "\n".join(formatted)
