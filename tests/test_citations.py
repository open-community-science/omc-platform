"""Test citation resolution using PubMed search."""
import asyncio
import sys
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Unreachable address (port 9 = discard): LLM-dependent steps fail fast and take
# their deterministic fallback, so these stay offline. Same idiom as
# tests/test_codex_review_fixes.py.
NO_LLM = "http://127.0.0.1:9/v1"


def test_find_cite_contexts():
    """Extract [CITE] placeholders from manuscript text."""
    from ai.citation_resolver import find_cite_contexts

    text = """Harmful algal blooms pose significant threats to freshwater ecosystems [CITE].
Among the most concerning organisms are cyanobacteria, particularly Microcystis [CITE].
CheckM2 has become the standard for MAG quality assessment [CITE: Parks et al 2022 CheckM2].
Long-read sequencing enables near-complete genome recovery [CITE]."""

    contexts = find_cite_contexts(text)
    assert len(contexts) == 4
    assert contexts[2]["hint"] == "Parks et al 2022 CheckM2"
    assert "Microcystis" in contexts[1]["sentence"]
    print(f"\nFound {len(contexts)} citation placeholders:")
    for c in contexts:
        print(f"  [{c['index']}] hint='{c['hint']}' sentence='{c['sentence'][:80]}...'")


@pytest.mark.timeout(30)
def test_generate_search_queries():
    """Generate PubMed search queries from citation contexts."""
    from ai.citation_resolver import find_cite_contexts, generate_search_queries

    text = """Harmful algal blooms pose significant threats to freshwater ecosystems [CITE].
CheckM2 has become the standard for MAG quality assessment [CITE: Parks et al 2022].
GTDB-Tk provides genome-based taxonomy for prokaryotes [CITE]."""

    contexts = find_cite_contexts(text)
    # generate_search_queries is async (it asks an LLM, falling back to keyword
    # extraction when none is reachable). Either path must yield exactly one
    # query per placeholder — that invariant is what this asserts.
    queries = asyncio.run(
        generate_search_queries(contexts, pipeline_type="nanopore_mag", base_url=NO_LLM)
    )
    assert len(queries) == 3
    print(f"\nGenerated {len(queries)} search queries:")
    for i, q in enumerate(queries):
        print(f"  [{i}] {q}")


def test_format_bibtex():
    """Format a PubMed article as BibTeX."""
    from ai.citation_resolver import format_bibtex_entry

    article = {
        "title": "CheckM2: a rapid, scalable and accurate tool for assessing microbial genome quality",
        "authors": ["Chklovski A", "Parks DH", "Woodcroft BJ", "Tyson GW"],
        "journal": "Nature Methods",
        "year": "2023",
        "volume": "20",
        "pages": "1203-1212",
        "doi": "10.1038/s41592-023-01940-w",
        "pmid": "37500758",
    }

    bib = format_bibtex_entry(article, "chklovski2023checkm2")
    assert "@article{chklovski2023checkm2" in bib
    assert "CheckM2" in bib
    assert "10.1038/s41592-023-01940-w" in bib
    print(f"\n{bib}")
