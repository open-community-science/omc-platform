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
