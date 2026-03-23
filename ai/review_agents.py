"""AI review agents for automated paper review.

Each agent reviews a different aspect and returns structured feedback
with an uncertainty index (0-1) indicating confidence in each comment.
"""
import asyncio
import json
from functools import partial
from .llm_client import get_client, chat


async def _achat(client, system, user, model=None, max_tokens=2000, temperature=0.5):
    """Run the synchronous chat() in a thread pool to avoid blocking the event loop."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        partial(chat, client, system, user, model=model, max_tokens=max_tokens, temperature=temperature),
    )


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

    response = await _achat(client, REVIEW_SYSTEM, f"""Review this microbial ecology manuscript for statistical issues:

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

    response = await _achat(client, REVIEW_SYSTEM, f"""Review this microbial ecology manuscript for methodological issues:

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

    response = await _achat(client, REVIEW_SYSTEM, f"""Review this manuscript for clarity and readability:

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


async def completeness_review(
    manuscript: dict,
    results_data: dict,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> dict:
    """Review manuscript completeness — all required sections, figures, data availability."""
    client = get_client(base_url=base_url, api_key=api_key)

    full_text = _truncate_manuscript(manuscript, max_chars=8000)
    data_summary = json.dumps(
        {k: type(v).__name__ if not isinstance(v, (str, int, float, bool)) else v
         for k, v in (results_data or {}).items()},
        indent=2, default=str,
    )[:1500] if results_data else 'Not available'

    response = await _achat(client, REVIEW_SYSTEM, f"""Review this microbial ecology manuscript for COMPLETENESS:

{full_text}

AVAILABLE PIPELINE DATA KEYS:
{data_summary}

Check for:
1. Are all standard sections present? (Abstract, Introduction, Methods, Results, Discussion)
2. Are all figures and tables referenced in the text actually described?
3. Is there a Data Availability statement with accession numbers?
4. Are all pipeline outputs mentioned in the results? Compare the data keys above to what's discussed.
5. Are sample sizes, sequencing depth, and quality metrics reported?
6. Are software versions and database versions cited in Methods?
7. Is there an author contributions section or acknowledgments?
8. Are key findings from Results reflected in the Abstract and Discussion?

This review is about MISSING content, not quality. Flag anything that should be there but isn't.

Respond in the JSON format specified.""",
        model=model, max_tokens=2000, temperature=0.5)

    return _parse_review(response, "completeness")


async def biological_plausibility_review(
    manuscript: dict,
    results_data: dict,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> dict:
    """Review biological plausibility — do the results make ecological sense?"""
    client = get_client(base_url=base_url, api_key=api_key)

    results_text = (manuscript.get('results', '') or '')[:4000]
    discussion_text = (manuscript.get('discussion', '') or '')[:3000]
    methods_text = (manuscript.get('methods', '') or '')[:2000]
    data_text = json.dumps(results_data, indent=2, default=str)[:2000] if results_data else 'Not available'

    response = await _achat(client, REVIEW_SYSTEM, f"""Review this microbial ecology manuscript for BIOLOGICAL PLAUSIBILITY:

METHODS:
{methods_text}

RESULTS:
{results_text}

DISCUSSION:
{discussion_text}

PIPELINE DATA:
{data_text}

You are an expert microbial ecologist. Check for:
1. Do the reported taxa make ecological sense for the stated environment?
   - Flag unexpected taxa that could indicate contamination (e.g., human skin microbes in deep ocean)
   - Flag known kit contaminants (Bradyrhizobium, Ralstonia, etc.) if they appear as major findings
2. Are diversity values plausible for the environment type?
   - Shannon diversity <1 in a complex environment is suspicious
   - Very high diversity in a host-associated sample may indicate contamination
3. Are MAG quality claims reasonable?
   - >99% completeness with 0% contamination should be flagged as possibly chimeric
   - Genome sizes far outside expected range for the claimed taxon
4. Do claimed metabolic capabilities match the organism's known biology?
5. Are any results likely artifacts of the bioinformatics pipeline?
   - Assembly artifacts, chimeric sequences, database misannotation
6. Are ecological interpretations supported by the data?
   - Correlation claimed as causation
   - Over-interpretation of relative abundance data

Be specific about which taxa or claims concern you, and explain what the expected biology would be.

Respond in the JSON format specified.""",
        model=model, max_tokens=2500, temperature=0.5)

    return _parse_review(response, "biological_plausibility")


async def citation_review(
    manuscript: dict,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> dict:
    """Review citation usage — do references support claims, are key refs missing?"""
    client = get_client(base_url=base_url, api_key=api_key)

    full_text = _truncate_manuscript(manuscript, max_chars=8000)

    response = await _achat(client, REVIEW_SYSTEM, f"""Review this microbial ecology manuscript for CITATION quality:

{full_text}

Check for:
1. Are major claims supported by citations? Flag unsupported assertions.
2. Are there key references missing that any reviewer in microbial ecology would expect?
   - Foundational methods papers (e.g., DADA2, MetaBAT2, GTDB-Tk, CheckM2)
   - Seminal ecology papers relevant to the study system
   - Recent reviews that provide context
3. Are any cited references potentially outdated? (e.g., citing old taxonomy when GTDB exists)
4. Are self-citations excessive or appropriate?
5. Does the Introduction establish sufficient context with references?
6. Are software tools cited with versions in Methods?
7. Are [CITE] or [CITATION NEEDED] placeholders still present? (These indicate the AI draft
   didn't resolve all citations — flag each one.)

For missing references, suggest specific papers or topics to search for when possible.
For example: "The claim about SAR11 dominance should cite Giovannoni (2017) Science or
Morris et al. (2002) Nature."

Respond in the JSON format specified.""",
        model=model, max_tokens=2500, temperature=0.5)

    return _parse_review(response, "citation")


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
    reviews.append(await completeness_review(manuscript, results_data, **kwargs))
    reviews.append(await biological_plausibility_review(manuscript, results_data, **kwargs))
    reviews.append(await citation_review(manuscript, **kwargs))

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
