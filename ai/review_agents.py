"""AI review agents for automated paper review.

Each agent reviews a different aspect and returns structured feedback
with an uncertainty index (0-1) indicating confidence in each comment.
"""
import json
from .llm_client import get_client, chat


REVIEW_SYSTEM = """You are a peer reviewer for microbial ecology manuscripts.
Your goal is to help authors improve their work through constructive, educational feedback.
For each issue you identify, explain WHY it matters so the author learns something.

For every comment, rate your confidence from 0.0 (very uncertain) to 1.0 (certain).
Low confidence flags should still be raised — they highlight areas worth the author's attention
even if you're not sure there's a problem.

Respond in JSON format:
{
  "comments": [
    {
      "section": "methods|results|discussion|introduction|general",
      "issue": "Brief description of the issue",
      "detail": "Educational explanation of why this matters and how to address it",
      "severity": "critical|major|minor|suggestion",
      "confidence": 0.0-1.0
    }
  ],
  "summary": "One paragraph overall assessment"
}
"""


async def statistical_review(
    manuscript: dict,
    results_data: dict,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> dict:
    """Review statistical methods and claims."""
    client = get_client(base_url=base_url, api_key=api_key)

    methods_text = (manuscript.get('methods', '') or '')[:3000]
    results_text = (manuscript.get('results', '') or '')[:3000]
    data_text = json.dumps(results_data, indent=2, default=str)[:2000] if results_data else 'Not available'

    response = chat(client, REVIEW_SYSTEM, f"""Review this microbial ecology manuscript for statistical issues:

METHODS:
{methods_text}

RESULTS:
{results_text}

PIPELINE DATA:
{data_text}

Check for:
1. Whether the pipeline's statistical tests are appropriate for the data type
2. Multiple testing corrections where needed
3. Sample size adequacy
4. Effect sizes vs just p-values
5. Correct interpretation of statistics
6. Missing analyses that would strengthen the paper

Respond in the JSON format specified.""",
        model=model, max_tokens=2000, temperature=0.5)

    return _parse_review(response, "statistical")


async def methodological_review(
    manuscript: dict,
    pipeline_config: dict,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> dict:
    """Review methodological rigor and reproducibility."""
    client = get_client(base_url=base_url, api_key=api_key)

    response = chat(client, REVIEW_SYSTEM, f"""Review this microbial ecology manuscript for methodological issues:

METHODS:
{manuscript.get('methods', '')}

PIPELINE CONFIGURATION:
{json.dumps(pipeline_config, indent=2, default=str) if pipeline_config else 'Not available'}

Check for:
1. Sufficient detail for reproducibility
2. Appropriate pipeline parameters for the data type
3. Missing methodological details (primers, thresholds, databases used)
4. Potential biases or confounders
5. Quality control steps (chimera removal, denoising, contamination checks)
6. Whether the approach matches current best practices

Respond in the JSON format specified.""",
        model=model, max_tokens=2000, temperature=0.5)

    return _parse_review(response, "methodological")


def _truncate_manuscript(manuscript: dict, max_chars: int = 6000) -> str:
    """Build manuscript text, truncating sections proportionally to fit context."""
    sections = list(manuscript.items())
    full_text = "\n\n".join(f"# {s.title()}\n{c}" for s, c in sections)
    if len(full_text) <= max_chars:
        return full_text
    # Proportionally truncate each section
    per_section = max_chars // max(len(sections), 1)
    return "\n\n".join(
        f"# {s.title()}\n{c[:per_section]}{'... [truncated]' if len(c) > per_section else ''}"
        for s, c in sections
    )


async def clarity_review(
    manuscript: dict,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> dict:
    """Review writing clarity and structure."""
    client = get_client(base_url=base_url, api_key=api_key)

    full_text = _truncate_manuscript(manuscript)

    response = chat(client, REVIEW_SYSTEM, f"""Review this manuscript for clarity and readability:

{full_text}

Check for:
1. Clear logical flow between sections and paragraphs
2. Jargon that needs explanation for a broad microbiology audience
3. Ambiguous statements or unsupported claims
4. Redundancy between sections
5. Whether the abstract accurately reflects the content
6. Missing context that would help the reader

Respond in the JSON format specified.""",
        model=model, max_tokens=2000, temperature=0.5)

    return _parse_review(response, "clarity")


async def run_all_reviews(
    manuscript: dict,
    results_data: dict,
    pipeline_config: dict,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> list:
    """Run all automated reviews and return combined feedback."""
    kwargs = dict(base_url=base_url, api_key=api_key, model=model)

    reviews = []
    reviews.append(await statistical_review(manuscript, results_data, **kwargs))
    reviews.append(await methodological_review(manuscript, pipeline_config, **kwargs))
    reviews.append(await clarity_review(manuscript, **kwargs))

    return reviews


def _parse_review(response_text: str, review_type: str) -> dict:
    """Parse a review response, handling both JSON and plain text."""
    try:
        # Try to extract JSON from response
        text = response_text.strip()
        # Handle markdown code blocks
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        if text.startswith("{"):
            parsed = json.loads(text)
            parsed["review_type"] = review_type
            return parsed
    except (ValueError, json.JSONDecodeError):
        pass

    # Fallback: wrap plain text as a single comment
    return {
        "review_type": review_type,
        "comments": [{
            "section": "general",
            "issue": "Review feedback",
            "detail": response_text,
            "severity": "suggestion",
            "confidence": 0.5,
        }],
        "summary": response_text[:500],
    }
