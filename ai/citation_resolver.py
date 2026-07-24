"""Resolve [CITE] placeholders in manuscripts using PubMed search.

Takes manuscript text with [CITE] markers, uses the surrounding context
to search PubMed for relevant references, and replaces placeholders
with proper citations. Generates a BibTeX bibliography.
"""
import asyncio
import re
import json
import logging
from functools import partial
from .llm_client import get_client, chat

log = logging.getLogger(__name__)

# Match [CITE] or [CITE: hint text]
_CITE_RE = re.compile(r'\[CITE(?::\s*([^\]]+))?\]')


def find_cite_contexts(text: str, window: int = 200) -> list[dict]:
    """Extract [CITE] placeholders with surrounding context.

    Returns list of {index, hint, before, after, sentence} dicts.
    """
    contexts = []
    for i, match in enumerate(_CITE_RE.finditer(text)):
        start, end = match.span()
        hint = match.group(1) or ""
        before = text[max(0, start - window):start].strip()
        after = text[end:end + window].strip()

        # Try to extract the full sentence containing the citation
        sent_start = text.rfind('.', max(0, start - 300), start)
        sent_end = text.find('.', end, min(len(text), end + 300))
        sentence = text[sent_start + 1 if sent_start >= 0 else max(0, start - 150):
                        sent_end + 1 if sent_end >= 0 else min(len(text), end + 150)].strip()

        contexts.append({
            "index": i,
            "hint": hint,
            "before": before[-150:],
            "after": after[:150],
            "sentence": sentence,
            "span": (start, end),
        })
    return contexts


async def generate_search_queries(
    contexts: list[dict],
    pipeline_type: str = "",
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> list[str]:
    """Use LLM to generate PubMed search queries from citation contexts."""
    if not contexts:
        return []

    client = get_client(base_url=base_url, api_key=api_key)

    # Batch contexts into one prompt
    context_text = "\n\n".join([
        f"Citation {c['index']+1}:\n"
        f"  Sentence: {c['sentence']}\n"
        f"  Hint: {c['hint'] or 'none'}"
        for c in contexts[:15]  # Limit to 15 citations per batch
    ])

    loop = asyncio.get_event_loop()
    try:
        response = await loop.run_in_executor(None, partial(chat, client,
            "You are a scientific literature search expert for microbial ecology.",
            f"""For each citation placeholder below, generate a PubMed search query
that would find the most relevant reference paper.

{context_text}

Return a JSON array of search queries, one per citation, in order.
Each query should be a focused PubMed search string (2-6 key terms).
Example: ["metagenome-assembled genome quality CheckM", "nanopore long-read metagenomics review"]

Return ONLY the JSON array.""",
            model=model, max_tokens=1000, temperature=0.3,
        ))
    except Exception as e:
        # No LLM reachable — degrade to keyword extraction so resolution still runs
        log.warning(f"Query generation LLM call failed, using fallback: {e}")
        return [_fallback_query(c) for c in contexts]

    try:
        text = response.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        queries = json.loads(text)
        if isinstance(queries, list):
            return [str(q) for q in queries]
    except (json.JSONDecodeError, ValueError):
        log.warning(f"Failed to parse search queries: {response[:200]}")

    # Fallback: extract key terms from sentences
    return [_fallback_query(c) for c in contexts]


def _fallback_query(context: dict) -> str:
    """Generate a simple search query from context when LLM fails."""
    hint = context.get("hint", "")
    if hint:
        return hint
    sentence = context.get("sentence", "")
    # Extract likely search terms (capitalized words, technical terms)
    words = [w for w in sentence.split() if len(w) > 3]
    return " ".join(words[:5])


def format_bibtex_entry(article: dict, key: str) -> str:
    """Format a PubMed article as a BibTeX entry."""
    authors = article.get("authors", [])
    author_str = " and ".join(authors[:10])
    if len(authors) > 10:
        author_str += " and others"

    doi = article.get("doi", "")
    pmid = article.get("pmid", "")

    return f"""@article{{{key},
  title = {{{article.get('title', '')}}},
  author = {{{author_str}}},
  journal = {{{article.get('journal', '')}}},
  year = {{{article.get('year', '')}}},
  volume = {{{article.get('volume', '')}}},
  pages = {{{article.get('pages', '')}}},
  doi = {{{doi}}},
  pmid = {{{pmid}}},
}}"""


def format_inline_citation(article: dict, key: str) -> str:
    """Format an inline citation like (Author et al., Year).

    Handles PubMed-style names ("Smith J") and standard ("J Smith").
    """
    authors = article.get("authors", [])
    year = article.get("year", "")
    if not authors:
        return f"[@{key}]"
    first = _surname(authors[0])
    if len(authors) == 1:
        return f"({first}, {year})"
    elif len(authors) == 2:
        second = _surname(authors[1])
        return f"({first} & {second}, {year})"
    else:
        return f"({first} et al., {year})"


def _surname(name: str) -> str:
    """Extract surname from author name in any common format.

    PubMed: "Smith J" → "Smith", standard: "J Smith" → "Smith".
    """
    if not name:
        return "Unknown"
    parts = name.split()
    if len(parts) == 1:
        return parts[0]
    # If last part is a single letter/initial, surname is first part (PubMed style)
    if len(parts[-1]) <= 2:
        return parts[0]
    # Otherwise surname is last part
    return parts[-1]


async def refine_query(
    context: dict,
    previous_query: str,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> str | None:
    """Reformulate a PubMed query that returned no results.

    Returns a new, broader query string, or None to stop refining (either the
    model declined or no LLM is reachable). Used to give each citation slot a
    second chance before falling back to a bare placeholder.
    """
    client = get_client(base_url=base_url, api_key=api_key)
    loop = asyncio.get_event_loop()
    try:
        response = await loop.run_in_executor(None, partial(chat, client,
            "You are a scientific literature search expert. Broaden or correct failed PubMed queries.",
            f"""This PubMed query returned no results:

    "{previous_query}"

It was meant to find a reference for this claim:
"{context.get('sentence', '')}"
Hint: {context.get('hint') or 'none'}

Suggest ONE broader or corrected PubMed query (2-5 key terms).
Return ONLY the query string — no quotes, no explanation.
If you cannot meaningfully improve it, return the single word STOP.""",
            model=model, max_tokens=40, temperature=0.3,
        ))
    except Exception as e:
        log.warning(f"Query refinement LLM call failed: {e}")
        return None

    q = (response or "").strip().strip('"').strip()
    if not q or q.upper().startswith("STOP") or q.lower() == previous_query.lower():
        return None
    return q


async def select_citation(
    context: dict,
    candidates: list[dict],
    already_cited: list[str] | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> dict | None:
    """Pick the best-fitting candidate article for a citation slot.

    Returns the chosen article dict, or None if no candidate genuinely supports
    the claim (better to leave a placeholder than cite the wrong paper — this is
    the anti-fabrication early-stop). With a single candidate, or when no LLM is
    reachable, degrades to the top-ranked result without an extra LLM call.
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    client = get_client(base_url=base_url, api_key=api_key)
    listing = "\n".join(
        f"{i}. {c.get('title', '')} — "
        f"{', '.join(c.get('authors', [])[:3])} ({c.get('year', '')}) "
        f"{c.get('journal', '')}"
        for i, c in enumerate(candidates)
    )
    already = "; ".join(t for t in (already_cited or []) if t) or "none"
    loop = asyncio.get_event_loop()
    try:
        response = await loop.run_in_executor(None, partial(chat, client,
            "You are a scientific literature expert selecting the single most relevant "
            "citation. Never force a citation that does not genuinely support the claim.",
            f"""A manuscript needs a citation for this claim:

"{context.get('sentence', '')}"
Hint: {context.get('hint') or 'none'}

Candidate papers from PubMed:
{listing}

Already cited elsewhere (prefer reusing one of these if it fits equally well): {already}

Reply with ONLY the number of the single best-fitting candidate, or the word
NONE if none of them genuinely support this specific claim.""",
            model=model, max_tokens=10, temperature=0.0,
        ))
    except Exception as e:
        log.warning(f"Citation selection LLM call failed, using top result: {e}")
        return candidates[0]

    text = (response or "").strip().upper()
    if text.startswith("NONE"):
        return None
    match = re.search(r"\d+", text)
    if match:
        idx = int(match.group())
        if 0 <= idx < len(candidates):
            return candidates[idx]
    return candidates[0]


VERIFIED = "supported"
UNSUPPORTED = "unsupported"
UNVERIFIABLE = "unknown"


async def verify_claim_in_abstract(
    context: dict,
    article: dict,
    abstract: str,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> str:
    """Check whether an article's abstract actually supports the cited claim.

    Returns one of :data:`VERIFIED`, :data:`UNSUPPORTED`, :data:`UNVERIFIABLE`.

    Selection (:func:`select_citation`) only ever sees titles, authors and
    journals, because esummary carries no abstracts — so a paper can look like a
    perfect fit and still not contain the claim. This is the gate that reads what
    the paper actually says before its name is attached to a sentence (issue #22).

    The three-way result matters: only :data:`UNSUPPORTED` means "this paper does
    not back this claim" and should block the citation. :data:`UNVERIFIABLE`
    covers "we could not tell" — PubMed holds no abstract, or no LLM was
    reachable — which is not evidence against the paper, and is reported
    separately so the caller can decide how strict to be.
    """
    if not abstract.strip():
        return UNVERIFIABLE

    client = get_client(base_url=base_url, api_key=api_key)
    loop = asyncio.get_event_loop()
    try:
        response = await loop.run_in_executor(None, partial(chat, client,
            "You are a meticulous scientific fact-checker. You judge only what a "
            "paper's abstract states, never what is plausible or what you already "
            "believe about the topic.",
            f"""Does this paper's abstract support the specific claim below?

CLAIM (from the manuscript):
"{context.get('sentence', '')}"

PAPER: {article.get('title', '')} ({article.get('year', '')})
ABSTRACT:
{abstract[:4000]}

Answer YES only if the abstract states, measures, or directly evidences this
claim. General topical overlap is NOT support — a paper about the same organism
or method does not support a claim it never makes. If the abstract is about a
different question, or is too general to bear on the claim, answer NO.

Reply with ONLY the word YES or NO.""",
            model=model, max_tokens=5, temperature=0.0,
        ))
    except Exception as e:
        # No LLM reachable: we learn nothing about the paper either way.
        log.warning(f"Citation claim-verification LLM call failed: {e}")
        return UNVERIFIABLE

    text = (response or "").strip().upper()
    if text.startswith("YES"):
        return VERIFIED
    if text.startswith("NO"):
        return UNSUPPORTED
    return UNVERIFIABLE


class CitationLibrary:
    """Accumulates cited articles, deduplicating by DOI / PMID / title.

    Guarantees the same paper is never added twice (even if two [CITE] slots
    resolve to it) and that cite keys are unique and stable across the run.
    Only articles passed to :meth:`add` — i.e. real search results — ever enter
    the bibliography.
    """

    def __init__(self):
        self._by_id: dict[str, str] = {}          # dedup id -> cite_key
        self._entries: dict[str, tuple[dict, str]] = {}  # cite_key -> (article, bibtex)
        self._order: list[str] = []               # cite_key insertion order

    @staticmethod
    def _dedup_ids(article: dict) -> list[str]:
        """All identifiers this article can be matched on.

        Indexing under every available id (not just a single preferred one)
        makes dedup robust to inconsistent PubMed metadata — e.g. the same
        paper returned once with a DOI and once with only a PMID.
        """
        ids = []
        doi = (article.get("doi") or "").strip().lower()
        if doi:
            ids.append(f"doi:{doi}")
        pmid = (article.get("pmid") or "").strip()
        if pmid:
            ids.append(f"pmid:{pmid}")
        if not ids:
            title = re.sub(r"[^a-z0-9]", "", (article.get("title") or "").lower())
            if title:
                ids.append(f"title:{title}")
        return ids

    def _make_key(self, article: dict) -> str:
        authors = article.get("authors", [])
        first = re.sub(r"[^a-z0-9]", "", _surname(authors[0]).lower()) if authors else ""
        first = first or "unknown"
        year = re.sub(r"[^0-9]", "", article.get("year", "")) or "nd"
        title_word = "ref"
        for w in (article.get("title", "") or "").split():
            wclean = re.sub(r"[^a-z0-9]", "", w.lower())
            if len(wclean) > 2:
                title_word = wclean
                break
        base = f"{first}{year}{title_word}"
        key, suffix = base, ord("b")
        while key in self._entries:
            key = f"{base}{chr(suffix)}"
            suffix += 1
        return key

    def add(self, article: dict) -> str:
        """Add an article (or return the existing key if already cited)."""
        ids = self._dedup_ids(article)
        for did in ids:
            if did in self._by_id:
                key = self._by_id[did]
                # register any identifiers this occurrence adds
                for other in ids:
                    self._by_id.setdefault(other, key)
                return key
        key = self._make_key(article)
        for did in ids:
            self._by_id[did] = key
        self._entries[key] = (article, format_bibtex_entry(article, key))
        self._order.append(key)
        return key

    def article_for(self, key: str) -> dict:
        return self._entries[key][0]

    def titles(self) -> list[str]:
        return [article.get("title", "") for article, _ in self._entries.values()]

    def bibtex(self) -> str:
        return "\n\n".join(self._entries[k][1] for k in self._order)

    def __len__(self) -> int:
        return len(self._entries)
