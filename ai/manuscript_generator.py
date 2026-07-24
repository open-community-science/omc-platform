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


# Outline-then-fill (two-phase drafting). Benchmarking showed that asking a model
# to outline first, tying each bullet to a specific data value, then expand the
# outline, substantially reduces fabricated numbers on smaller models (e.g.
# gpt-oss-20b: 6 unsupported numbers single-pass -> 1-2 outline-first) without
# hurting stronger models. Only the DRAFT is kept; the outline never reaches the
# manuscript.
_OUTLINE_INSTRUCTION = """

First, produce ONLY a nested bullet-point outline of this section. Each bullet must be a
specific point grounded in a value from the data/context above (name the number or fact).
Do not write prose yet — just the outline."""


def _draft_from_outline_instruction(outline: str) -> str:
    return f"""

Here is an approved outline for this section:

{outline}

Now write the finished section as flowing scientific prose that follows this outline.
Output the prose ONLY — no bullet points, no outline, no headers like "Outline"/"Draft"."""


async def _draft_section(client, system, prompt, model=None, max_tokens=20000,
                         on_token=None, outline_first=False):
    """Draft one section, optionally via an outline-then-fill two-phase pass.

    outline_first=True runs two calls: (1) a cheap, non-streamed outline whose
    bullets are tied to specific data values, then (2) the streamed prose draft
    grounded in that outline. Only the draft is returned — the outline is scratch
    scaffolding to reduce fabrication and is never shown or stored.
    """
    if not outline_first:
        return await _achat(client, system, prompt, model=model,
                            max_tokens=max_tokens, on_token=on_token)
    outline = await _achat(client, system, prompt + _OUTLINE_INSTRUCTION,
                           model=model, max_tokens=2000, on_token=None)
    return await _achat(client, system, prompt + _draft_from_outline_instruction(outline),
                        model=model, max_tokens=max_tokens, on_token=on_token)


SYSTEM_PROMPT = """You are a scientific writing assistant for microbial ecology research.
You help generate clear, accurate manuscript drafts based on bioinformatics pipeline outputs
and author-provided context. Write in a professional academic style suitable for publication.

FRAMING (default stance) — unless the author's interview/context says otherwise, treat this
as a REANALYSIS of an existing (often public) dataset that you are reporting on as an
independent scientist. Under this stance:
- Do NOT assume or invent the original authors' intent, motivation, or study design.
- Do NOT claim hypotheses were formulated in advance or that samples were collected to test a
  particular question. Frame the work as investigating what the data reveal, not as testing a
  preregistered hypothesis.
- Report honestly what the reanalysis found, as any other scientist writing up a reanalysis
  would — grounded in the data, not in a presumed backstory.
- Where the interview DOES provide genuine study aims, context, or hypotheses, use them and
  frame accordingly; the reanalysis stance is only the default when that context is absent.
- Assume there are NO human co-authors unless the interview/context says otherwise. The paper
  must stand on its own — never address, defer to, or leave editorial notes for a human author.
  Do NOT emit bracketed asides like "[AUTHOR: describe the study system]" or "[X]". Where a
  piece of information genuinely isn't available, either omit it or state the gap plainly in the
  prose as a limitation (e.g. "the sampling environment is not described in the available
  metadata"). ([CITE] placeholders for citations are the ONLY bracketed markers allowed.)

Key principles:
- Be precise and accurate about the data
- Acknowledge limitations; be candid about incomplete or low-quality data rather
  than presenting it as complete and solid (honest caveats help the author)
- TONE — collegial and matter-of-fact. Report labeling errors, mislabeled samples,
  contamination, primer/target mix-ups, or batch quirks the way a considerate colleague
  would: as routine, good-faith observations. A mislabel is almost always an honest
  accident (a slip in a big submission), not carelessness — note it plainly, in neutral
  language, and move on. Do NOT editorialize, express surprise or disapproval, dwell on it,
  or imply the original workers were sloppy. It is data to be aware of, not a scandal.
- Use appropriate hedging language for interpretations
- Follow standard scientific paper structure
- Include placeholders [CITE] where citations would be needed

CRITICAL — never fabricate. The study's subject matter comes ONLY from the STUDY
context you are given:
- Do NOT invent the research topic, organisms, environment, or study system. If the
  STUDY section is missing or thin, describe only what the data show and, if the study
  system is unknown, say so plainly in prose — do NOT guess and do NOT leave an [AUTHOR: …]
  note (there may be no human author to address).
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
2. State the question(s) this analysis investigates (as a reanalysis of the dataset,
   unless the author context above indicates a preplanned study with prior hypotheses —
   do not manufacture a hypothesis that isn't supported by that context)
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


def _format_autoresearch_findings(interview_data: dict) -> str:
    """Autoresearch claims as grounding for the write-up, with background on how they
    were produced. An autonomous agent explored the data, ran its own analysis code,
    and recorded claims; a verification pass re-executed each. VERIFIED claims are the
    trustworthy numbers; the rest are provided too (labeled) for the writer to judge —
    they are NOT pre-excluded. Returns '' when no run exists."""
    ar = (interview_data or {}).get("_autoresearch") or {}
    all_claims = ar.get("claims", [])
    verified = [c for c in all_claims if c.get("verdict") == "verified"]
    other = [c for c in all_claims if c.get("verdict") != "verified"]
    assumptions = ar.get("assumptions", [])
    if not all_claims and not assumptions:
        return ""
    lines = []
    for c in verified:
        kind = c.get("kind", "observation")
        stmt = (c.get("statement") or "").strip()
        val = str(c.get("value", "")).strip()
        lines.append(f"- [{kind}] {stmt}" + (f"  — value: {val}" if val else ""))
    block = (
        "VERIFIED FINDINGS FROM AUTONOMOUS ANALYSIS (autoresearch):\n"
        "An analysis agent explored this dataset — it proposed a research agenda, wrote and\n"
        "ran its own analysis code, and recorded claims. EACH claim below was then\n"
        "INDEPENDENTLY VERIFIED by re-executing its computation (or reconciled against the\n"
        "re-run evidence), so these numbers are trustworthy and reproducible. Items marked\n"
        "[quality_caveat] are real limitations to report honestly.\n\n"
        + ("\n".join(lines) if lines else "(none fully verified)") +
        "\n\nThese are the most reliable numbers you have. You have NO computational ability\n"
        "of your own, so do not compute, re-derive, round, or estimate — when you report a\n"
        "quantitative result, use the values above verbatim. You need not report every\n"
        "finding, and their order here is NOT their importance: weigh them by biological\n"
        "significance. The community's dominant/characteristic taxa and the main ecological\n"
        "patterns are the headline; methodological confounds, depth artifacts, and metadata\n"
        "quirks are secondary support. Lead with the biology and give the rest proportionate\n"
        "weight."
    )
    if other:
        olines = []
        for c in other:
            verdict = c.get("verdict", "unverifiable")
            stmt = (c.get("statement") or "").strip()
            val = str(c.get("value", "")).strip()
            rec = (c.get("reconcile") or {}).get("reasoning", "").strip()
            line = f"- [{verdict}] {stmt}" + (f"  — claimed value: {val}" if val else "")
            if rec:
                line += f"  [re-check: {rec[:200]}]"
            olines.append(line)
        block += (
            "\n\nOTHER FINDINGS THE ANALYSIS FLAGGED (did NOT cleanly reproduce on independent\n"
            "re-execution — verdict and any re-check note shown). These are NOT pre-excluded:\n"
            "YOU decide whether and how to use each. Two common, genuinely useful cases live\n"
            "here — a NEGATIVE result that couldn't be positively confirmed (e.g. 'no significant\n"
            "effect of X'), and a result the re-check only PARTIALLY confirmed. Don't state their\n"
            "specific numbers as established fact, but don't silently drop them either: report\n"
            "what's warranted, honestly hedged (e.g. as a non-significant or unconfirmed result).\n\n"
            + "\n".join(olines)
        )
    if assumptions:
        alines = []
        for a in assumptions:
            stmt = (a.get("statement") or "").strip()
            why = (a.get("why") or "").strip()
            impact = (a.get("impact") or "").strip()
            extra = "; ".join(x for x in [why, (f"if wrong: {impact}" if impact else "")] if x)
            alines.append(f"- {stmt}" + (f"  ({extra})" if extra else ""))
        block += (
            "\n\nASSUMPTIONS THE ANALYSIS MADE (things it could not confirm and had to assume):\n"
            + "\n".join(alines) +
            "\n\nAny result that depends on one of these assumptions must be stated with the\n"
            "assumption made explicit (report them as caveats), not as settled fact."
        )
    return block


def build_results_prompt(study_context: str, pipeline_outputs: dict, interview_data: dict) -> str:
    """User prompt for the Results section."""
    findings = _format_autoresearch_findings(interview_data)
    if findings:
        source_block = (
            f"{findings}\n\n"
            "RAW PIPELINE OUTPUTS (secondary — QC/intermediate numbers, often inconsistent "
            "with each other; use ONLY the verified findings above for any number, and do not "
            "quote sample/read counts from here):\n"
            f"{json.dumps(pipeline_outputs, indent=2, default=str) if pipeline_outputs else 'None.'}")
    else:
        source_block = (
            "PIPELINE OUTPUTS:\n"
            f"{json.dumps(pipeline_outputs, indent=2, default=str) if pipeline_outputs else 'No pipeline outputs available yet.'}")

    return f"""Write the Results section for this study.

STUDY (authoritative — the paper is about THIS and nothing else):
{study_context}

{source_block}

RESEARCH QUESTION:
{interview_data.get('research_question', 'Not specified')}

Write it as a microbial-ecology Results narrative — NOT a QC report. Perspective and concision matter:

1. LEAD WITH THE BIG PICTURE. Open with the community's biological identity and headline
   story: what the community IS, what DOMINATES it (name the dominant/characteristic taxa and
   their abundances), and the main ecological pattern. A reader should grasp the central
   biological finding in the first paragraph.
2. STRUCTURAL / METHODOLOGICAL FEATURES ARE FRAMING, NOT THE STORY. Dataset structure — an
   assay or batch design, paired/mixed runs, sequencing-depth artifacts, metadata
   inconsistencies — is context to state ONCE, briefly (a sentence or two), early, then move
   on. Do NOT organize the Results around such a feature, re-litigate it, or let any single
   anomaly or QC point consume more than a small fraction of the section. The biology is the
   story; a structural quirk is a caveat, not the headline.
3. BE CONCISE AND SELECTIVE. Do NOT catalogue every finding. Choose the most important, state
   each once, and build a tight, well-organized narrative — a few clear paragraphs (community
   & dominant taxa → main ecological patterns → key caveats), not an exhaustive or repetitive
   enumeration. A focused Results beats a comprehensive one.
4. Do NOT open with ASV totals, per-rank classification counts, or read-retention rates as if
   they were a result — a clause of context at most.
5. For every number, use the verified findings verbatim (you cannot compute your own).
6. Reference figures/tables where natural; report genuine quality caveats plainly, near the end.

Report, don't interpret — save interpretation for Discussion."""


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
        return ("(No study metadata available — do NOT guess the subject matter; describe only "
                "what the data show and state plainly that the study system is not described in "
                "the available metadata. Do not leave an [AUTHOR: …] note.)")
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
    cite_model: str | None = None,
    outline_first: bool = False,
    on_progress=None,
) -> dict:
    """
    Generate a manuscript draft from pipeline outputs and author interview.

    model: model used for drafting the sections.
    cite_model: model used for citation resolution (issue #21) — falls back to
                `model` when unset, so the high-volume citation loop can run on a
                cheaper/local model than the drafting.
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
    sections["introduction"] = await _draft_section(client, SYSTEM_PROMPT,
        build_introduction_prompt(interview_context, study_context, bioproject_accession, pipeline_type),
        model=model, max_tokens=20000, on_token=stream_tokens, outline_first=outline_first)

    await emit("done", f"Introduction complete ({len(sections['introduction'])} chars)")

    # Methods
    await emit("start", "Generating Methods...")
    log.info("Generating methods...")
    sections["methods"] = await _draft_section(client, SYSTEM_PROMPT,
        build_methods_prompt(pipeline_type, study_context, bioproject_accession, interview_data, results_summary),
        model=model, max_tokens=20000, on_token=stream_tokens, outline_first=outline_first)

    await emit("done", f"Methods complete ({len(sections['methods'])} chars)")

    # Results
    await emit("start", "Generating Results...")
    log.info("Generating results...")
    sections["results"] = await _draft_section(client, SYSTEM_PROMPT,
        build_results_prompt(study_context, pipeline_outputs, interview_data),
        model=model, max_tokens=30000, on_token=stream_tokens, outline_first=outline_first)

    await emit("done", f"Results complete ({len(sections['results'])} chars)")

    # Discussion
    await emit("start", "Generating Discussion...")
    log.info("Generating discussion...")
    sections["discussion"] = await _draft_section(client, SYSTEM_PROMPT,
        build_discussion_prompt(study_context, sections['results'], interview_data),
        model=model, max_tokens=30000, on_token=stream_tokens, outline_first=outline_first)

    await emit("done", f"Discussion complete ({len(sections['discussion'])} chars)")

    # Abstract (written last, based on all sections)
    await emit("start", "Generating Abstract...")
    log.info("Generating abstract...")
    sections["abstract"] = await _draft_section(client, SYSTEM_PROMPT,
        build_abstract_prompt(study_context, sections),
        model=model, max_tokens=5000, on_token=stream_tokens, outline_first=outline_first)
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
            model=cite_model or model,
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


def _make_searcher(search_fn, candidates_per_query: int):
    """Wrap a search callable so it always returns a list, awaiting coroutines.

    Passes the candidate count only when the callable accepts it, so both the
    real ``search_pubmed(query, max_results)`` and 1-arg test mocks work.
    """
    try:
        accepts_count = len(inspect.signature(search_fn).parameters) >= 2
    except (TypeError, ValueError):
        accepts_count = False

    async def run(query):
        result = search_fn(query, candidates_per_query) if accepts_count else search_fn(query)
        if inspect.isawaitable(result):
            result = await result
        return result or []

    return run


async def resolve_citations(
    sections: dict,
    pipeline_type: str = "",
    search_fn=None,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    max_query_rounds: int = 2,
    candidates_per_query: int = 5,
) -> tuple[dict, str]:
    """Resolve [CITE] placeholders in manuscript sections.

    For each [CITE] slot this runs a small grounded loop (adapted from
    AI-Scientist's citation harness): search PubMed, and if nothing comes back
    reformulate the query and try again up to ``max_query_rounds``, stopping as
    soon as candidates are found. When several candidates return, the model
    selects the best fit (or declines, leaving a placeholder rather than citing
    the wrong paper). All chosen articles flow through a
    :class:`CitationLibrary` that deduplicates by DOI/PMID/title, so the same
    paper is never added twice and only real search results enter the
    bibliography. Returns the updated sections and a BibTeX bibliography string.

    search_fn: optional callable(query[, max_results]) -> list[dict] for
               testing/mocking. Each dict should have title, authors, journal,
               year, doi, pmid.
    """
    from .citation_resolver import CitationLibrary, refine_query, select_citation, _fallback_query

    # Combine all sections into one text for context extraction
    full_text = "\n\n".join(
        f"## {name}\n{content}" for name, content in sections.items()
    )

    contexts = find_cite_contexts(full_text)
    if not contexts:
        return sections, ""

    # Generate an initial search query per placeholder
    queries = await generate_search_queries(
        contexts,
        pipeline_type=pipeline_type,
        base_url=base_url,
        api_key=api_key,
        model=model,
    )

    search = _make_searcher(search_fn, candidates_per_query) if search_fn else None
    library = CitationLibrary()
    edits = []  # (start, end, replacement) into the ORIGINAL full_text

    for i, ctx in enumerate(contexts):
        # queries only covers the first batch of contexts; fall back for the rest
        query = queries[i] if i < len(queries) and queries[i] else _fallback_query(ctx)

        candidates = []
        if search:
            for round_idx in range(max_query_rounds):
                try:
                    candidates = await search(query)
                except Exception as e:
                    log.warning(f"Search failed for query '{query}': {e}")
                    candidates = []
                await asyncio.sleep(0.5)  # NCBI rate limit
                if candidates:
                    break  # early stop for this slot — we found matches
                if round_idx < max_query_rounds - 1:
                    new_query = await refine_query(
                        ctx, query, base_url=base_url, api_key=api_key, model=model
                    )
                    if not new_query:
                        break  # model declined to broaden — give up on this slot
                    query = new_query

        inline = None
        if candidates:
            chosen = await select_citation(
                ctx, candidates, already_cited=library.titles(),
                base_url=base_url, api_key=api_key, model=model,
            )
            if chosen:
                cite_key = library.add(chosen)
                inline = format_inline_citation(library.article_for(cite_key), cite_key)

        if inline is None:
            # No result, or the model declined to cite — leave a readable placeholder
            hint = ctx.get("hint", "")
            inline = f"[{hint or f'ref{i+1}'}]"
            log.info(f"No citation resolved for placeholder {i+1}: '{query}'")

        edits.append((ctx["span"][0], ctx["span"][1], inline))

    # Apply by span, right-to-left, so identical placeholder strings (bare [CITE])
    # each get THEIR chosen citation. A text-based replace(original, ..., 1) would
    # always hit the first remaining occurrence and mis-assign papers (issue #34).
    for start, end, replacement in sorted(edits, key=lambda e: e[0], reverse=True):
        full_text = full_text[:start] + replacement + full_text[end:]

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

    return updated_sections, library.bibtex()


# ---------------------------------------------------------------------------
# Revise loop (issue #20) — feed review findings + deterministic checks back
# into a grounded section rewrite, the OMC analog of AI-Scientist's reflection.
# ---------------------------------------------------------------------------

REVISE_INSTRUCTIONS = SYSTEM_PROMPT + """

You are now REVISING a single section to address specific reviewer and
automated-check findings. Rules:
- Fix only what the findings call out. Preserve all other content, facts, and numbers.
- Never invent data, citations, numbers, software versions, or study details to
  satisfy a finding. If a finding asks for information you do not have, state the gap
  plainly in the prose as a limitation — do not guess, and do not leave an [AUTHOR: …]
  note (assume no human co-author to address unless the context says otherwise).
- Keep unresolved literature references as bare [CITE] placeholders.
- Do not delete content merely to satisfy a length or completeness note — add the
  missing content, or if it isn't available, say so plainly in the prose.

Return ONLY the revised section text — no preamble, no code fences.
If no change is warranted, return exactly: NO CHANGES NEEDED
"""


def flatten_review_comments(reviews) -> list[dict]:
    """Flatten run_all_reviews() output into a flat issue list for revision."""
    issues = []
    for review in reviews or []:
        rtype = review.get("review_type", "review")
        for c in review.get("comments", []) or []:
            issues.append({
                "section": (c.get("section") or "general").lower(),
                "issue": c.get("issue", ""),
                "detail": c.get("detail", ""),
                "severity": c.get("severity", "suggestion"),
                "confidence": c.get("confidence", 0.5),
                "source": rtype,
            })
    return issues


def _strip_code_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n", "", t)
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


def _build_revise_prompt(section, text, issues, general_issues, study_metadata=None):
    lines = [f"Section: {section}", "", "Current text:", text, "", "Findings to address:"]
    for n, i in enumerate(issues, 1):
        lines.append(f"{n}. [{i.get('severity', '')}] {i.get('issue', '')} — {i.get('detail', '')}")
    if general_issues:
        lines += ["", "Manuscript-wide findings (address only if they affect this section):"]
        for i in general_issues:
            lines.append(f"- [{i.get('severity', '')}] {i.get('issue', '')} — {i.get('detail', '')}")
    if study_metadata:
        lines += ["", "STUDY CONTEXT (do not contradict or exceed this):", _format_study(study_metadata)]
    lines += ["", f"Rewrite the {section} section addressing these findings, following all rules."]
    return "\n".join(lines)


async def revise_manuscript(
    sections: dict,
    reviews=None,
    check_issues=None,
    *,
    results_data=None,
    available_figures=None,
    study_metadata=None,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    max_passes: int = 2,
    on_progress=None,
) -> tuple[dict, list]:
    """Revise sections to address review findings + deterministic checks.

    Groups findings by section, rewrites each affected section, and re-runs the
    deterministic checks between passes — stopping early for a section once its
    deterministic issues clear or the model reports no change (bounded by
    ``max_passes``). Returns ``(revised_sections, change_log)``. Degrades to the
    original sections when no LLM is reachable and never mutates the input dict.
    """
    from .manuscript_checks import run_all_checks

    revised = dict(sections)
    change_log: list[dict] = []

    async def emit(msg):
        if on_progress:
            await on_progress(msg)

    # Reviewer findings + deterministic checks, merged into one issue list
    issues = flatten_review_comments(reviews)
    if check_issues is None:
        check_issues = run_all_checks(sections, results_data, available_figures)
    issues += check_issues
    if not issues:
        return revised, change_log

    section_names = set(revised.keys())
    general = [i for i in issues if i.get("section") not in section_names]
    by_section: dict[str, list] = {}
    for i in issues:
        sec = i.get("section")
        if sec in section_names:
            by_section.setdefault(sec, []).append(i)

    client = get_client(base_url=base_url, api_key=api_key)

    for sec, sec_issues in by_section.items():
        changed = False
        passes = 0
        remaining = sec_issues
        errored = None
        for _ in range(max_passes):
            if not remaining:
                break
            passes += 1
            prompt = _build_revise_prompt(sec, revised[sec], remaining, general, study_metadata)
            try:
                response = await _achat(client, REVISE_INSTRUCTIONS, prompt, model=model, max_tokens=8000)
            except Exception as e:
                log.warning(f"Revise LLM call failed for '{sec}': {e}")
                errored = str(e)
                break
            text = _strip_think(response or "").strip()
            if not text or text.upper().startswith("NO CHANGES NEEDED"):
                break
            text = _strip_code_fences(text)
            if text and text != revised[sec]:
                revised[sec] = text
                changed = True
            # Re-run deterministic checks scoped to this section for early-stop
            recheck = run_all_checks({sec: revised[sec]}, results_data, available_figures)
            remaining = [i for i in recheck if i.get("section") == sec]

        entry = {"section": sec, "changed": changed, "passes": passes,
                 "issues": len(sec_issues), "remaining_checks": len(remaining)}
        if errored:
            entry["error"] = errored
        change_log.append(entry)
        await emit(f"Revised {sec} ({'changed' if changed else 'no change'})")

    return revised, change_log
