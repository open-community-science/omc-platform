"""AI-powered manuscript generation from pipeline outputs and author interview."""
import json
from .llm_client import get_client, chat


SYSTEM_PROMPT = """You are a scientific writing assistant for microbial ecology research.
You help generate clear, accurate manuscript drafts based on bioinformatics pipeline outputs
and author-provided context. Write in a professional academic style suitable for publication.

Key principles:
- Be precise and accurate about the data
- Acknowledge limitations
- Use appropriate hedging language for interpretations
- Follow standard scientific paper structure
- Include placeholders [CITE] where citations would be needed
"""


async def generate_manuscript_draft(
    pipeline_outputs: dict,
    interview_data: dict,
    pipeline_type: str,
    bioproject_accession: str,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> dict:
    """
    Generate a manuscript draft from pipeline outputs and author interview.

    Returns a dict with sections: abstract, introduction, methods, results, discussion
    """
    client = get_client(base_url=base_url, api_key=api_key)

    results_summary = _summarize_results(pipeline_outputs)
    interview_context = _format_interview(interview_data)

    sections = {}

    # Introduction
    sections["introduction"] = chat(client, SYSTEM_PROMPT, f"""Generate an Introduction section for a paper with this context:

RESEARCH CONTEXT FROM AUTHOR:
{interview_context}

BioProject: {bioproject_accession}
Pipeline: {pipeline_type}

Write 2-3 paragraphs that:
1. Establish the scientific context and importance
2. State the research question/hypothesis
3. Briefly preview the approach

Use [CITE] placeholders where literature citations would go.""",
        model=model, max_tokens=2000)

    # Methods
    sections["methods"] = chat(client, SYSTEM_PROMPT, f"""Generate a Methods section for this analysis:

Pipeline: {pipeline_type}
BioProject: {bioproject_accession}

AUTHOR CONTEXT ON SAMPLES:
{interview_data.get('study_context', 'Not provided')}
{interview_data.get('sample_info', 'Not provided')}

PIPELINE OUTPUTS:
{results_summary}

Write a Methods section covering:
1. Data acquisition (SRA/BioProject)
2. Sequence processing pipeline
3. Analysis parameters (inferred from outputs)
4. Statistical approaches used

Be specific about tools and versions where inferable.""",
        model=model, max_tokens=2000)

    # Results
    sections["results"] = chat(client, SYSTEM_PROMPT, f"""Generate a Results section based on these pipeline outputs:

{json.dumps(pipeline_outputs, indent=2, default=str) if pipeline_outputs else 'No pipeline outputs available yet.'}

RESEARCH QUESTION:
{interview_data.get('research_question', 'Not specified')}

Write a Results section that:
1. Reports findings objectively without interpretation
2. References figures and tables (Figure 1, Table 1, etc.)
3. Includes key statistics and quantitative findings
4. Follows a logical flow from overview to specific findings

Do not interpret results - save that for Discussion.""",
        model=model, max_tokens=3000)

    # Discussion
    sections["discussion"] = chat(client, SYSTEM_PROMPT, f"""Generate a Discussion section:

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

Use [CITE] placeholders for literature references.""",
        model=model, max_tokens=3000)

    # Abstract (written last, based on all sections)
    sections["abstract"] = chat(client, SYSTEM_PROMPT, f"""Write an abstract for this manuscript:

INTRODUCTION (summary):
{sections['introduction'][:500]}

METHODS (summary):
{sections['methods'][:500]}

KEY RESULTS:
{sections['results'][:500]}

DISCUSSION (summary):
{sections['discussion'][:500]}

Write a single paragraph (200-300 words) covering:
1. Background and objective
2. Methods
3. Key results
4. Conclusions""",
        model=model, max_tokens=500)

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
