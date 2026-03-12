"""Test PubMed search and citation resolution end-to-end."""
import sys
import asyncio
import pytest

sys.path.insert(0, "/data/omc/omc-platform")


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_pubmed_search():
    """Direct PubMed E-utilities search returns structured metadata."""
    from ai.pubmed_search import search_pubmed

    results = await search_pubmed("CheckM2 genome quality", max_results=2)
    assert len(results) >= 1
    article = results[0]
    assert "title" in article
    assert "authors" in article
    assert len(article["authors"]) > 0
    assert article["pmid"]
    print(f"\n{article['authors'][0]} ({article['year']}) {article['title'][:80]}")


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_resolve_citations_with_pubmed():
    """Full pipeline: extract [CITE] → LLM query → PubMed search → BibTeX."""
    from ai.manuscript_generator import resolve_citations
    from ai.pubmed_search import search_pubmed

    sections = {
        "introduction": "Harmful algal blooms threaten freshwater ecosystems [CITE]. "
                         "CheckM2 has become the standard for MAG quality [CITE: Chklovski 2023 CheckM2].",
        "methods": "We used GTDB-Tk for taxonomic classification [CITE].",
    }

    # Use a mock search to avoid rate limits and LLM dependency
    async def mock_search(query):
        return [{
            "title": f"Mock paper for: {query}",
            "authors": ["Smith J", "Doe A"],
            "journal": "Mock Journal",
            "year": "2023",
            "volume": "1",
            "pages": "1-10",
            "doi": "10.1234/mock",
            "pmid": "12345678",
        }]

    updated, bibliography = await resolve_citations(
        sections,
        pipeline_type="nanopore_mag",
        search_fn=mock_search,
    )

    # All [CITE] should be replaced
    full = " ".join(updated.values())
    assert "[CITE" not in full, f"Unresolved citations remain: {full}"
    assert "(Smith" in full
    assert "@article{" in bibliography
    print(f"\nBibliography:\n{bibliography[:500]}")
    print(f"\nIntro: {updated['introduction'][:200]}")
