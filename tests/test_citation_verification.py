"""Citation claim-verification against abstracts (issue #22).

Deterministic and offline: no live LLM, no NCBI. The verifier's LLM call is
monkeypatched so each test states exactly what the fact-checker concluded, and
abstracts are injected rather than fetched.
"""
import asyncio
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai import citation_resolver as cr
from ai import manuscript_generator as mg
from ai.pubmed_search import _abstract_text

ARTICLE = {
    "title": "Pelagibacter abundance in coastal surface water",
    "authors": ["Smith J"], "year": "2021", "journal": "J Mar Microbiol",
    "volume": "3", "pages": "1-9", "doi": "10.1/x", "pmid": "111",
}
CLAIM = {"sentence": "SAR11 dominates surface ocean bacterioplankton.", "hint": ""}


def _verdict_of(answer, abstract="SAR11 comprised 35% of surface reads."):
    """Run the gate with the fact-checker forced to reply `answer`."""
    cr_chat = lambda *a, **k: answer
    orig, cr.chat = cr.chat, cr_chat
    try:
        return asyncio.run(cr.verify_claim_in_abstract(CLAIM, ARTICLE, abstract))
    finally:
        cr.chat = orig


# ── the gate itself ───────────────────────────────────────────────────────────

def test_abstract_supporting_the_claim_is_accepted():
    assert _verdict_of("YES") == cr.VERIFIED


def test_abstract_not_supporting_the_claim_is_rejected():
    assert _verdict_of("NO") == cr.UNSUPPORTED


def test_missing_abstract_is_unverifiable_not_a_rejection():
    # An absent abstract says nothing about the paper, so it must not read as
    # evidence against it.
    assert _verdict_of("YES", abstract="   ") == cr.UNVERIFIABLE


def test_unparseable_verifier_reply_is_unverifiable():
    assert _verdict_of("possibly, it depends") == cr.UNVERIFIABLE


def test_verifier_failure_is_unverifiable_not_a_rejection():
    def boom(*a, **k):
        raise RuntimeError("no LLM")
    orig, cr.chat = cr.chat, boom
    try:
        v = asyncio.run(cr.verify_claim_in_abstract(CLAIM, ARTICLE, "some abstract"))
    finally:
        cr.chat = orig
    assert v == cr.UNVERIFIABLE


# ── the gate wired into resolve_citations ─────────────────────────────────────

SECTIONS = {"Results": "SAR11 dominates surface ocean bacterioplankton [CITE]."}


def _resolve(answer):
    """resolve_citations with search, abstracts and the fact-checker all stubbed."""
    async def search_fn(query, max_results=5):
        return [ARTICLE]

    async def fetch_abstracts_fn(pmids, cache=None):
        cache = cache if cache is not None else {}
        for p in pmids:
            cache[str(p)] = "SAR11 comprised 35% of surface reads."
        return dict(cache)

    orig, cr.chat = cr.chat, (lambda *a, **k: answer)
    try:
        return asyncio.run(mg.resolve_citations(
            SECTIONS, search_fn=search_fn, fetch_abstracts_fn=fetch_abstracts_fn,
        ))
    finally:
        cr.chat = orig


def test_verified_citation_reaches_the_manuscript():
    sections, bib = _resolve("YES")
    assert "Smith" in sections["Results"]
    assert "[CITE]" not in sections["Results"]
    assert "Pelagibacter abundance" in bib


def test_unsupported_citation_leaves_a_placeholder_and_empty_bibliography():
    sections, bib = _resolve("NO")
    # The paper must not be cited, and must not enter the bibliography either.
    assert "Smith" not in sections["Results"]
    assert "Pelagibacter abundance" not in bib
    assert "[ref1]" in sections["Results"]


def test_gate_can_be_disabled():
    async def search_fn(query, max_results=5):
        return [ARTICLE]

    def fail(*a, **k):
        raise AssertionError("verifier must not run when verify_claims=False")

    orig, cr.chat = cr.chat, fail
    try:
        sections, _bib = asyncio.run(mg.resolve_citations(
            SECTIONS, search_fn=search_fn, verify_claims=False,
        ))
    finally:
        cr.chat = orig
    assert "Smith" in sections["Results"]


def test_unhealthy_verifier_is_given_up_on_instead_of_timing_out_per_citation():
    """A broken verifier costs one LLM timeout per placeholder; stop paying it.

    Every call here has an abstract to judge and still yields no usable verdict,
    which means the verifier — not the paper — is the problem. After two of those
    the gate switches off for the rest of the run, so a long manuscript doesn't
    serialise dozens of timeouts.
    """
    calls = []

    async def search_fn(query, max_results=5):
        return [ARTICLE]

    async def fetch_abstracts_fn(pmids, cache=None):
        cache = cache if cache is not None else {}
        for p in pmids:
            cache[str(p)] = "some abstract text"
        return dict(cache)

    def broken(*a, **k):
        calls.append(1)
        raise RuntimeError("verifier down")

    sections = {"Results": "One [CITE]. Two [CITE]. Three [CITE]. Four [CITE]. Five [CITE]."}
    orig, cr.chat = cr.chat, broken
    try:
        asyncio.run(mg.resolve_citations(
            sections, search_fn=search_fn, fetch_abstracts_fn=fetch_abstracts_fn,
        ))
    finally:
        cr.chat = orig

    # generate_search_queries also goes through chat and fails first, so allow
    # for it; the point is that verification stops early rather than firing for
    # all five placeholders.
    assert len(calls) < 5, f"verifier kept being called after failing: {len(calls)} calls"


# ── efetch XML parsing ────────────────────────────────────────────────────────

def test_structured_abstract_keeps_section_labels():
    xml = """<PubmedArticle><MedlineCitation><PMID>111</PMID><Article><Abstract>
      <AbstractText Label="BACKGROUND">SAR11 is abundant.</AbstractText>
      <AbstractText Label="RESULTS">It reached <i>35</i>% of reads.</AbstractText>
    </Abstract></Article></MedlineCitation></PubmedArticle>"""
    text = _abstract_text(ET.fromstring(xml))
    assert "BACKGROUND: SAR11 is abundant." in text
    # inline markup must contribute its text rather than truncating the section
    assert "RESULTS: It reached 35% of reads." in text


def test_article_without_abstract_yields_empty_string():
    xml = "<PubmedArticle><MedlineCitation><PMID>222</PMID></MedlineCitation></PubmedArticle>"
    assert _abstract_text(ET.fromstring(xml)) == ""
