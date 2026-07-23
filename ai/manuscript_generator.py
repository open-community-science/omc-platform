"""AI-powered manuscript generation from pipeline outputs and author interview."""
import asyncio
import inspect
import json
import logging
import re
from functools import partial
from .llm_client import get_client, chat, _strip_think, _effective_tokens, DEFAULT_MODEL
from .citation_resolver import find_cite_contexts, generate_search_queries, format_bibtex_entry, format_inline_citation

log = logging.getLogger(__name__)


async def _achat(client, system, user, model=None, max_tokens=20000, on_token=None):
    """Generate a chat completion, optionally streaming tokens via on_token callback.

    on_token: async callable(chunk_text) called for each streamed chunk.
    Returns the full response text.
    """
    if on_token is None:
        # Non-streaming path — run in thread pool
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            partial(chat, client, system, user, model=model, max_tokens=max_tokens),
        )

    # Streaming path — use OpenAI streaming API in a thread, push chunks to async queue
    m = model or DEFAULT_MODEL
    tokens = _effective_tokens(max_tokens, m)

    loop = asyncio.get_event_loop()
    queue = asyncio.Queue()

    def _stream_in_thread():
        try:
            stream = client.chat.completions.create(
                model=m,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=tokens,
                temperature=0.7,
                stream=True,
            )
            for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    loop.call_soon_threadsafe(queue.put_nowait, delta.content)
            loop.call_soon_threadsafe(queue.put_nowait, None)  # sentinel
        except Exception as e:
            loop.call_soon_threadsafe(queue.put_nowait, e)

    thread_future = loop.run_in_executor(None, _stream_in_thread)

    full_text = []
    while True:
        item = await queue.get()
        if item is None:
            break
        if isinstance(item, Exception):
            raise item
        full_text.append(item)
        await on_token(item)

    await thread_future  # ensure thread is done
    result = "".join(full_text)
    return _strip_think(result)


SYSTEM_PROMPT = """You are a scientific writing assistant for microbial ecology research.
You help generate clear, accurate manuscript drafts based on bioinformatics pipeline outputs
and author-provided context. Write in a professional academic style suitable for publication.

Key principles:
- Be precise and accurate about the data
- Acknowledge limitations
- Use appropriate hedging language for interpretations
- Follow standard scientific paper structure
- Include placeholders [CITE] where citations would be needed

CRITICAL — never fabricate. The study's subject matter comes ONLY from the STUDY
context you are given:
- Do NOT invent the research topic, organisms, environment, or study system. If the
  STUDY section is missing or thin, describe only what the data show and write
  [AUTHOR: describe the study system] rather than guessing.
- Do NOT invent citations, author names, or years. Every reference must be a bare
  [CITE] placeholder — never "(Smith et al., 2022)".
- Do NOT invent software versions, database releases, or parameters. If a version
  isn't given, write the tool name without a version.
- Do NOT state numbers that aren't in the provided results. Never fill in
  placeholder values like [X] with guesses.
"""


def build_introduction_prompt(interview_context: str, study_context: str,
                              bioproject_accession: str, pipeline_type: str) -> str:
    """User prompt for the Introduction section (paired with SYSTEM_PROMPT)."""
    return f"""Generate an Introduction section for a paper with this context:

RESEARCH CONTEXT FROM AUTHOR:
{interview_context}

STUDY (authoritative — the paper is about THIS and nothing else):
{study_context}

BioProject: {bioproject_accession}
Pipeline: {pipeline_type}

Write 2-3 paragraphs that:
1. Establish the scientific context and importance
2. State the research question/hypothesis
3. Briefly preview the approach

Use [CITE] placeholders where literature citations would go."""


def build_methods_prompt(pipeline_type: str, study_context: str, bioproject_accession: str,
                         interview_data: dict, results_summary: str) -> str:
    """User prompt for the Methods section."""
    return f"""Generate a Methods section for this analysis:

Pipeline: {pipeline_type}
STUDY (authoritative — the paper is about THIS and nothing else):
{study_context}

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

Be specific about tools and versions where inferable."""


def build_results_prompt(study_context: str, pipeline_outputs: dict, interview_data: dict) -> str:
    """User prompt for the Results section."""
    return f"""Generate a Results section based on these pipeline outputs:

STUDY (authoritative — the paper is about THIS and nothing else):
{study_context}

{json.dumps(pipeline_outputs, indent=2, default=str) if pipeline_outputs else 'No pipeline outputs available yet.'}

RESEARCH QUESTION:
{interview_data.get('research_question', 'Not specified')}

Write a Results section that:
1. Reports findings objectively without interpretation
2. References figures and tables (Figure 1, Table 1, etc.)
3. Includes key statistics and quantitative findings
4. Follows a logical flow from overview to specific findings

Do not interpret results - save that for Discussion."""


def build_discussion_prompt(study_context: str, results_text: str, interview_data: dict) -> str:
    """User prompt for the Discussion section."""
    return f"""Generate a Discussion section:

STUDY (authoritative — the paper is about THIS and nothing else):
{study_context}

KEY RESULTS:
{results_text[:1500]}

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


def build_abstract_prompt(study_context: str, sections: dict) -> str:
    """User prompt for the Abstract (written last, from the other sections)."""
    return f"""Write an abstract for this manuscript:

STUDY (authoritative — the paper is about THIS and nothing else):
{study_context}

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
4. Conclusions"""


def _format_study(study_metadata: dict | None) -> str:
    """Format the SRA/BioProject metadata that grounds the paper's subject matter.

    Without this the model only sees an accession number and invents a study
    system wholesale (a sea-ice amplicon run was written up as a forensic
    "thanatomicrobiome" study), so keep this in every section prompt.
    """
    if not study_metadata:
        return ("(No study metadata available — do NOT guess the subject matter; "
                "write [AUTHOR: describe the study system] instead.)")
    fields = [
        ("Title", study_metadata.get("title")),
        ("Organism", study_metadata.get("organism")),
        ("Organization", study_metadata.get("organization")),
        ("Samples", study_metadata.get("num_samples")),
        ("SRA runs", study_metadata.get("num_sra_runs")),
        ("Abstract/Description", study_metadata.get("description")),
    ]
    lines = [f"{k}: {v}" for k, v in fields if v]
    return "\n".join(lines) if lines else (
        "(No study metadata available — do NOT guess the subject matter.)"
    )


async def generate_manuscript_draft(
    pipeline_outputs: dict,
    interview_data: dict,
    pipeline_type: str,
    bioproject_accession: str,
    study_metadata: dict | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    on_progress=None,
) -> dict:
    """
    Generate a manuscript draft from pipeline outputs and author interview.

    on_progress: optional async callable(event, detail) for streaming progress.
    Returns a dict with sections: abstract, introduction, methods, results, discussion
    """
    client = get_client(base_url=base_url, api_key=api_key)

    async def emit(event, detail=""):
        if on_progress:
            await on_progress(event, detail)

    async def token_cb(chunk):
        """Forward streamed tokens to progress callback."""
        if on_progress:
            await on_progress("token", chunk)

    # Only stream tokens if we have a progress callback
    stream_tokens = token_cb if on_progress else None

    results_summary = _summarize_results(pipeline_outputs)
    interview_context = _format_interview(interview_data)
    study_context = _format_study(study_metadata)

    sections = {}

    # Introduction
    await emit("start", "Generating Introduction...")
    log.info(f"Generating introduction for {bioproject_accession}...")
    sections["introduction"] = await _achat(client, SYSTEM_PROMPT,
        build_introduction_prompt(interview_context, study_context, bioproject_accession, pipeline_type),
        model=model, max_tokens=20000, on_token=stream_tokens)

    await emit("done", f"Introduction complete ({len(sections['introduction'])} chars)")

    # Methods
    await emit("start", "Generating Methods...")
    log.info("Generating methods...")
    sections["methods"] = await _achat(client, SYSTEM_PROMPT,
        build_methods_prompt(pipeline_type, study_context, bioproject_accession, interview_data, results_summary),
        model=model, max_tokens=20000, on_token=stream_tokens)

    await emit("done", f"Methods complete ({len(sections['methods'])} chars)")

    # Results
    await emit("start", "Generating Results...")
    log.info("Generating results...")
    sections["results"] = await _achat(client, SYSTEM_PROMPT,
        build_results_prompt(study_context, pipeline_outputs, interview_data),
        model=model, max_tokens=30000, on_token=stream_tokens)

    await emit("done", f"Results complete ({len(sections['results'])} chars)")

    # Discussion
    await emit("start", "Generating Discussion...")
    log.info("Generating discussion...")
    sections["discussion"] = await _achat(client, SYSTEM_PROMPT,
        build_discussion_prompt(study_context, sections['results'], interview_data),
        model=model, max_tokens=30000, on_token=stream_tokens)

    await emit("done", f"Discussion complete ({len(sections['discussion'])} chars)")

    # Abstract (written last, based on all sections)
    await emit("start", "Generating Abstract...")
    log.info("Generating abstract...")
    sections["abstract"] = await _achat(client, SYSTEM_PROMPT,
        build_abstract_prompt(study_context, sections),
        model=model, max_tokens=5000, on_token=stream_tokens)
    await emit("done", f"Abstract complete ({len(sections['abstract'])} chars)")
    log.info("All sections generated.")

    # Resolve [CITE] placeholders via PubMed search (non-blocking)
    await emit("start", "Resolving citations via PubMed...")
    try:
        from .pubmed_search import search_pubmed
        sections, bibliography = await resolve_citations(
            sections,
            pipeline_type=pipeline_type,
            search_fn=search_pubmed,
            base_url=base_url,
            api_key=api_key,
            model=model,
        )
        if bibliography:
            sections["bibliography"] = bibliography
        await emit("done", "Citations resolved")
    except Exception as e:
        log.warning(f"Citation resolution failed (manuscript preserved): {e}")
        await emit("done", f"Citation resolution skipped: {e}")

    await emit("complete", f"Manuscript complete — {len(sections)} sections")
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


async def resolve_citations(
    sections: dict,
    pipeline_type: str = "",
    search_fn=None,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> tuple[dict, str]:
    """Resolve [CITE] placeholders in manuscript sections.

    For each [CITE], generates a PubMed search query, searches PubMed,
    and replaces the placeholder with an inline citation. Returns the
    updated sections and a BibTeX bibliography string.

    search_fn: optional callable(query) -> list[dict] for testing/mocking.
               Each dict should have title, authors, journal, year, doi, pmid.
    """
    # Combine all sections into one text for context extraction
    full_text = "\n\n".join(
        f"## {name}\n{content}" for name, content in sections.items()
    )

    contexts = find_cite_contexts(full_text)
    if not contexts:
        return sections, ""

    # Generate search queries from contexts
    queries = await generate_search_queries(
        contexts,
        pipeline_type=pipeline_type,
        base_url=base_url,
        api_key=api_key,
        model=model,
    )

    # Search PubMed for each query and collect results
    bibtex_entries = []
    replacements = []  # (original_text, replacement_text)

    for i, (ctx, query) in enumerate(zip(contexts, queries)):
        article = None
        if search_fn:
            try:
                result = search_fn(query)
                if inspect.isawaitable(result):
                    result = await result
                results = result
                if results:
                    article = results[0]
            except Exception as e:
                log.warning(f"Search failed for query '{query}': {e}")
            # Rate limit between searches
            await asyncio.sleep(0.5)

        if article:
            # Generate citation key: firstauthor2023keyword
            authors = article.get("authors", [])
            from .citation_resolver import _surname
            first_author = _surname(authors[0]).lower() if authors else "unknown"
            year = article.get("year", "")
            title_word = article.get("title", "").split()[0].lower() if article.get("title") else "ref"
            cite_key = f"{first_author}{year}{title_word}"

            inline = format_inline_citation(article, cite_key)
            bibtex = format_bibtex_entry(article, cite_key)
            bibtex_entries.append(bibtex)
        else:
            # No search result — leave a numbered placeholder
            cite_key = f"ref{i+1}"
            hint = ctx.get("hint", "")
            inline = f"[{hint or cite_key}]"
            log.info(f"No PubMed result for citation {i+1}: '{query}'")

        original = full_text[ctx["span"][0]:ctx["span"][1]]
        replacements.append((original, inline))

    # Apply replacements in reverse order to preserve spans
    for original, replacement in reversed(replacements):
        full_text = full_text.replace(original, replacement, 1)

    # Split back into sections
    updated_sections = {}
    for name in sections:
        marker = f"## {name}\n"
        if marker in full_text:
            start = full_text.index(marker) + len(marker)
            # Find next section or end
            next_markers = [full_text.index(f"## {n}\n") for n in sections if f"## {n}\n" in full_text and full_text.index(f"## {n}\n") > start]
            end = min(next_markers) if next_markers else len(full_text)
            updated_sections[name] = full_text[start:end].strip()
        else:
            updated_sections[name] = sections[name]

    bibliography = "\n\n".join(bibtex_entries)
    return updated_sections, bibliography
