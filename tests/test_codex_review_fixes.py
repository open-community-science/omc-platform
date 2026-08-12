"""Regression tests for the Codex review findings #32, #33, #34.

Deterministic and offline: no live LLM or network. LLM-dependent steps point at a
dead address so they fall back, and selection/search are mocked.
"""
import asyncio
import base64
import re
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

NO_LLM = "http://127.0.0.1:9/v1"


def _article(title, author, year):
    return {"title": title, "authors": [author], "year": str(year), "journal": "J",
            "volume": "1", "pages": "1-2", "doi": f"d{year}", "pmid": str(year)}


# ── #34: identical [CITE] placeholders keep their OWN citation ────────────────
def test_citations_not_swapped_between_identical_placeholders(monkeypatch):
    import ai.manuscript_generator as mg
    import ai.citation_resolver as cr

    alpha, beta = _article("Alpha", "Aaa", 2001), _article("Beta", "Bbb", 2002)

    def search_fn(query):                       # distinct paper per placeholder
        return [alpha] if "first" in query.lower() else [beta]

    async def fake_select(ctx, candidates, **kw):
        return candidates[0]
    monkeypatch.setattr(cr, "select_citation", fake_select)

    sections = {"results": "First claim [CITE]. Second claim [CITE]."}
    out, _bib = asyncio.run(mg.resolve_citations(sections, search_fn=search_fn, base_url=NO_LLM))
    text = out["results"]
    # Alpha (2001) must stay with the FIRST claim, Beta (2002) with the SECOND —
    # not swapped by a text-based replace() on identical '[CITE]' strings.
    assert "First claim (Aaa, 2001)" in text, text
    assert "Second claim (Bbb, 2002)" in text, text
    assert text.index("2001") < text.index("2002"), text


# ── #32: local backend keeps a served model id without a slash ────────────────
def test_local_backend_keeps_slashless_served_model(monkeypatch):
    import portal.app.llm_backends as lb
    from portal.app.database import User

    async def fake_list():
        return ["codeqwen3-14b", "qwen/qwen3-coder-30b"]
    monkeypatch.setattr(lb, "list_local_models", fake_list)

    user = User(llm_backend=lb.BACKEND_LOCAL, llm_model="codeqwen3-14b")
    res = asyncio.run(lb.resolve_llm(user))
    assert res["backend"] == lb.BACKEND_LOCAL
    assert res["model"] == "codeqwen3-14b"       # preserved, not swapped for the first


def test_local_backend_falls_back_when_saved_model_not_served(monkeypatch):
    import portal.app.llm_backends as lb
    from portal.app.database import User

    async def fake_list():
        return ["qwen/qwen3-coder-30b"]
    monkeypatch.setattr(lb, "list_local_models", fake_list)

    user = User(llm_backend=lb.BACKEND_LOCAL, llm_model="no-longer-served")
    res = asyncio.run(lb.resolve_llm(user))
    assert res["model"] == "qwen/qwen3-coder-30b"


# ── #33: metadata TSV uses the biological collection_date ─────────────────────
def test_metadata_prelude_uses_collection_date_not_first_created():
    from portal.app.slurm import _amplicon_metadata_prelude

    sub = types.SimpleNamespace(sample_metadata={"sample_records": [
        {"run_accession": "SRR1", "collection_date": "2025-06-15", "first_created": "2026-01-20"},
    ]})
    prelude, _args = _amplicon_metadata_prelude(sub)
    blob = re.search(r"printf '%s' '([A-Za-z0-9+/=]+)'", prelude).group(1)
    tsv = base64.b64decode(blob).decode()
    header, row = tsv.strip().split("\n")
    vals = dict(zip(header.split("\t"), row.split("\t")))
    assert vals["collection_date"] == "2025-06-15"   # sampling date, not record-creation
    assert vals["first_created"] == "2026-01-20"      # kept in its own column
