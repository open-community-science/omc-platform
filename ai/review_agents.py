"""AI review agents for automated paper review."""
from anthropic import Anthropic
from typing import Optional


async def statistical_review(api_key: str, manuscript: dict, results_data: dict) -> dict:
    """Review statistical methods and claims."""
    client = Anthropic(api_key=api_key)

    prompt = f"""Review this microbial ecology manuscript for statistical issues:

METHODS:
{manuscript.get('methods', '')}

RESULTS:
{manuscript.get('results', '')}

PIPELINE DATA:
{results_data}

Check for:
1. Appropriate statistical tests for the data type
2. Multiple testing corrections where needed
3. Sample size adequacy
4. Effect sizes vs just p-values
5. Correct interpretation of statistics

Provide specific, actionable feedback."""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )

    return {
        "review_type": "statistical",
        "comments": response.content[0].text,
        "severity": "suggestion",  # Could be: critical, major, minor, suggestion
    }


async def methodological_review(api_key: str, manuscript: dict, pipeline_config: dict) -> dict:
    """Review methodological rigor and reproducibility."""
    client = Anthropic(api_key=api_key)

    prompt = f"""Review this microbial ecology manuscript for methodological issues:

METHODS:
{manuscript.get('methods', '')}

PIPELINE CONFIGURATION:
{pipeline_config}

Check for:
1. Sufficient detail for reproducibility
2. Appropriate pipeline parameters
3. Missing methodological details
4. Potential biases or confounders
5. Quality control steps

Provide specific, actionable feedback."""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )

    return {
        "review_type": "methodological",
        "comments": response.content[0].text,
        "severity": "suggestion",
    }


async def clarity_review(api_key: str, manuscript: dict) -> dict:
    """Review writing clarity and structure."""
    client = Anthropic(api_key=api_key)

    full_text = "\n\n".join([
        f"# {section.title()}\n{content}"
        for section, content in manuscript.items()
    ])

    prompt = f"""Review this manuscript for clarity and readability:

{full_text}

Check for:
1. Clear logical flow
2. Jargon that needs explanation
3. Ambiguous statements
4. Redundancy
5. Missing transitions
6. Abstract/summary alignment with content

Provide specific, actionable suggestions to improve clarity."""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )

    return {
        "review_type": "clarity",
        "comments": response.content[0].text,
        "severity": "minor",
    }


async def run_all_reviews(
    api_key: str,
    manuscript: dict,
    results_data: dict,
    pipeline_config: dict,
) -> list:
    """Run all automated reviews and return combined feedback."""
    reviews = []

    reviews.append(await statistical_review(api_key, manuscript, results_data))
    reviews.append(await methodological_review(api_key, manuscript, pipeline_config))
    reviews.append(await clarity_review(api_key, manuscript))

    return reviews
