"""Tests for the iterative, deduplicating citation resolution (issue #19).

These are deterministic and need neither a live LLM nor network access: the
mock search callables stand in for PubMed, and the LLM-dependent steps
(query generation / refinement / selection) are pointed at a fast-fail address
so they degrade gracefully to their non-LLM fallbacks.
"""
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Fast-fail LLM endpoint: nothing listens here, so LLM calls raise immediately
# and the resolver falls back to its deterministic paths.
NO_LLM = "http://127.0.0.1:9/v1"


def _article(title, author, year, doi, pmid):
    return {
        "title": title, "authors": [author], "year": year,
        "journal": "J", "volume": "1", "pages": "1-2", "doi": doi, "pmid": pmid,
    }


# ---------------------------------------------------------------------------
# CitationLibrary — deduplication + stable, unique keys
# ---------------------------------------------------------------------------

def test_library_dedupes_same_paper():
    from ai.citation_resolver import CitationLibrary
    lib = CitationLibrary()
    art = _article("CheckM2 genome quality", "Chklovski A", "2023", "10.1/x", "111")
    k1 = lib.add(art)
    k2 = lib.add(dict(art))            # identical DOI → same entry
    k3 = lib.add({**art, "doi": ""})   # DOI dropped but same PMID → still same
    assert k1 == k2 == k3
    assert len(lib) == 1
    assert lib.bibtex().count("@article{") == 1


def test_library_distinct_papers_get_distinct_keys():
    from ai.citation_resolver import CitationLibrary
    lib = CitationLibrary()
    k1 = lib.add(_article("GTDB-Tk taxonomy", "Chaumeil P", "2019", "10.2/y", "222"))
    k2 = lib.add(_article("CheckM2 quality", "Chklovski A", "2023", "10.1/x", "111"))
    assert k1 != k2
    assert len(lib) == 2
    assert lib.bibtex().count("@article{") == 2


def test_library_key_collision_is_disambiguated():
    """Two different papers that would generate the same base key stay unique."""
    from ai.citation_resolver import CitationLibrary
    lib = CitationLibrary()
    a = _article("Genome analysis methods", "Smith J", "2020", "10/a", "1")
    b = _article("Genome analysis tools", "Smith J", "2020", "10/b", "2")
    ka, kb = lib.add(a), lib.add(b)     # both base to smith2020genome
    assert ka != kb
    assert len(lib) == 2


# ---------------------------------------------------------------------------
# select_citation — single-candidate and empty degrade without an LLM
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_select_single_candidate_needs_no_llm():
    from ai.citation_resolver import select_citation
    cand = [_article("X", "A B", "2020", "10/z", "9")]
    chosen = await select_citation({"sentence": "s"}, cand, base_url=NO_LLM)
    assert chosen is cand[0]


@pytest.mark.asyncio
async def test_select_no_candidates_returns_none():
    from ai.citation_resolver import select_citation
    assert await select_citation({"sentence": "s"}, [], base_url=NO_LLM) is None


# ---------------------------------------------------------------------------
# resolve_citations — dedup across slots, early stop, bounded rounds
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dedup_across_slots():
    """Three [CITE] slots that all resolve to the same paper yield one entry."""
    from ai.manuscript_generator import resolve_citations
    sections = {"introduction": "First claim [CITE]. Second [CITE]. Third [CITE]."}

    async def mock(query, n=5):
        return [_article("One Paper", "Smith J", "2023", "10.1/same", "999")]

    updated, bib = await resolve_citations(sections, search_fn=mock, base_url=NO_LLM)
    assert "[CITE" not in " ".join(updated.values())
    assert bib.count("@article{") == 1          # deduped to a single reference
    assert "(Smith, 2023)" in updated["introduction"]


@pytest.mark.asyncio
async def test_search_stops_on_first_hit():
    """A slot that gets a hit on round 1 does not trigger further search rounds."""
    from ai.manuscript_generator import resolve_citations
    calls = {"n": 0}

    async def mock(query, n=5):
        calls["n"] += 1
        return [_article("P", "Ada B", "2021", "10/z", "7")]

    await resolve_citations({"m": "x [CITE]."}, search_fn=mock,
                            max_query_rounds=3, base_url=NO_LLM)
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_empty_results_are_bounded_and_leave_placeholder():
    """With no LLM to refine a failed query, the loop stops after one round and
    leaves a readable placeholder rather than fabricating a citation."""
    from ai.manuscript_generator import resolve_citations
    calls = {"n": 0}

    async def mock(query, n=5):
        calls["n"] += 1
        return []

    updated, bib = await resolve_citations(
        {"m": "some claim [CITE: some hint]."},
        search_fn=mock, max_query_rounds=3, base_url=NO_LLM,
    )
    assert calls["n"] == 1                       # refine declined (no LLM) → bounded
    assert bib == ""                             # nothing fabricated
    assert "[some hint]" in updated["m"]


@pytest.mark.asyncio
async def test_no_search_fn_leaves_placeholders():
    """Anti-fabrication: without a search backend, nothing enters the bibliography."""
    from ai.manuscript_generator import resolve_citations
    updated, bib = await resolve_citations(
        {"m": "claim one [CITE]. claim two [CITE: mag quality]."},
        search_fn=None, base_url=NO_LLM,
    )
    assert bib == ""
    assert "[ref1]" in updated["m"]
    assert "[mag quality]" in updated["m"]
