"""PubMed search via NCBI E-utilities API.

Direct HTTP calls to esearch + esummary + efetch — no MCP dependency.
NCBI rate limit: 3 requests/second without API key, 10/s with key.
"""
import asyncio
import logging
import xml.etree.ElementTree as ET

import httpx

log = logging.getLogger(__name__)

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

# efetch accepts many ids per call; keep batches modest so one bad id or a slow
# response doesn't cost the whole set.
_ABSTRACT_BATCH = 20


async def search_pubmed(query: str, max_results: int = 3) -> list[dict]:
    """Search PubMed and return article metadata.

    Returns list of dicts with: title, authors, journal, year, volume, pages, doi, pmid.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        # Step 1: Search for PMIDs
        search_resp = await client.get(ESEARCH_URL, params={
            "db": "pubmed",
            "term": query,
            "retmax": max_results,
            "retmode": "json",
            "sort": "relevance",
        })
        search_resp.raise_for_status()
        search_data = search_resp.json()

        id_list = search_data.get("esearchresult", {}).get("idlist", [])
        if not id_list:
            return []

        # Rate limit: NCBI allows 3 req/s without API key
        await asyncio.sleep(0.4)

        # Step 2: Fetch summaries for those PMIDs
        summary_resp = await client.get(ESUMMARY_URL, params={
            "db": "pubmed",
            "id": ",".join(id_list),
            "retmode": "json",
        })
        summary_resp.raise_for_status()
        summary_data = summary_resp.json()

        results = []
        for pmid in id_list:
            article = summary_data.get("result", {}).get(pmid, {})
            if not article or "error" in article:
                continue

            authors = [
                a.get("name", "") for a in article.get("authors", [])
            ]

            # Extract DOI from articleids
            doi = ""
            for aid in article.get("articleids", []):
                if aid.get("idtype") == "doi":
                    doi = aid.get("value", "")
                    break

            results.append({
                "title": article.get("title", ""),
                "authors": authors,
                "journal": article.get("fulljournalname", "") or article.get("source", ""),
                "year": article.get("pubdate", "")[:4],
                "volume": article.get("volume", ""),
                "pages": article.get("pages", ""),
                "doi": doi,
                "pmid": pmid,
            })

        return results


def _abstract_text(art: ET.Element) -> str:
    """Flatten one article's <AbstractText> nodes into plain text.

    Structured abstracts split into labelled sections (BACKGROUND, RESULTS, …);
    those labels are kept because they tell a claim-checker which part of the
    paper a sentence came from. `itertext()` is used so inline markup (<i>, <sup>)
    contributes its text instead of truncating the section.
    """
    parts = []
    for node in art.iter("AbstractText"):
        text = "".join(node.itertext()).strip()
        if not text:
            continue
        label = (node.get("Label") or node.get("NlmCategory") or "").strip()
        parts.append(f"{label}: {text}" if label else text)
    return "\n".join(parts).strip()


async def fetch_abstracts(pmids: list[str], cache: dict | None = None) -> dict[str, str]:
    """Fetch abstracts for PMIDs via efetch. Returns {pmid: abstract_text}.

    A PMID maps to "" when PubMed holds no abstract for it (common for older
    records, editorials and some letters) — that is a real answer, not a failure,
    so it is cached too and callers can tell it apart from "we never asked".

    `cache` is a caller-owned {pmid: abstract} dict, normally scoped to a single
    manuscript run: several [CITE] slots often resolve to the same paper, and
    NCBI shouldn't be asked twice. Passing a dict makes the lifetime explicit
    rather than hiding a module-global that would go stale across runs.
    """
    cache = cache if cache is not None else {}
    wanted = [p for p in dict.fromkeys(str(p) for p in pmids if p) if p not in cache]

    for i in range(0, len(wanted), _ABSTRACT_BATCH):
        batch = wanted[i:i + _ABSTRACT_BATCH]
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(EFETCH_URL, params={
                    "db": "pubmed",
                    "id": ",".join(batch),
                    "retmode": "xml",
                    "rettype": "abstract",
                })
                resp.raise_for_status()
                root = ET.fromstring(resp.text)
        except (httpx.HTTPError, ET.ParseError) as e:
            # Leave these PMIDs absent from the cache: unknown, not "no abstract".
            log.warning("efetch failed for %d PMID(s): %s", len(batch), e)
            continue

        for art in root.iter("PubmedArticle"):
            pmid_node = art.find(".//MedlineCitation/PMID")
            if pmid_node is None or not (pmid_node.text or "").strip():
                continue
            cache[pmid_node.text.strip()] = _abstract_text(art)

        # Record a definitive "no abstract" for anything the response omitted, so
        # a retry loop can't re-request ids PubMed simply has nothing for.
        for pmid in batch:
            cache.setdefault(pmid, "")

        if i + _ABSTRACT_BATCH < len(wanted):
            await asyncio.sleep(0.4)  # NCBI: 3 req/s without an API key

    return {p: cache[p] for p in (str(x) for x in pmids if x) if p in cache}
