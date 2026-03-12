"""PubMed search via NCBI E-utilities API.

Direct HTTP calls to esearch + esummary — no MCP dependency.
NCBI rate limit: 3 requests/second without API key, 10/s with key.
"""
import asyncio
import logging
import httpx

log = logging.getLogger(__name__)

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"


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
