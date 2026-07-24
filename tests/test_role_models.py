"""Tests for per-role model selection (issue #21).

Verifies the config-level override resolution and that the citation model is
threaded independently of the drafting model through generate_manuscript_draft.
"""
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

NO_LLM = "http://127.0.0.1:9/v1"


# ---------------------------------------------------------------------------
# Settings.role_model
# ---------------------------------------------------------------------------

def test_role_model_prefers_override_else_default():
    from portal.app.config import Settings
    s = Settings(llm_model="m-base", llm_model_cite="m-cheap", llm_model_review="m-rev")
    # explicit overrides win
    assert s.role_model("cite", "resolved") == "m-cheap"
    assert s.role_model("review", "resolved") == "m-rev"
    # no draft override → the caller's resolved model is used
    assert s.role_model("draft", "resolved") == "resolved"
    # unknown role → default
    assert s.role_model("nope", "resolved") == "resolved"


def test_role_model_defaults_are_empty_so_behaviour_is_unchanged():
    from portal.app.config import Settings
    s = Settings(llm_model="m-base")
    for role in ("draft", "cite", "review"):
        assert s.role_model(role, "resolved") == "resolved"


# ---------------------------------------------------------------------------
# cite_model threading through generate_manuscript_draft
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generate_forwards_cite_model(monkeypatch):
    import ai.manuscript_generator as mg

    async def fake_achat(client, system, user, model=None, max_tokens=20000, on_token=None):
        return "Section text."

    captured = {}

    async def fake_resolve(sections, **kwargs):
        captured["model"] = kwargs.get("model")
        return sections, ""

    monkeypatch.setattr(mg, "_achat", fake_achat)
    monkeypatch.setattr(mg, "resolve_citations", fake_resolve)

    await mg.generate_manuscript_draft(
        pipeline_outputs={}, interview_data={}, pipeline_type="nanopore_mag",
        bioproject_accession="PRJNA000", base_url=NO_LLM,
        model="draft-model", cite_model="cite-model",
    )
    assert captured["model"] == "cite-model"


@pytest.mark.asyncio
async def test_cite_model_falls_back_to_draft_model(monkeypatch):
    import ai.manuscript_generator as mg

    async def fake_achat(client, system, user, model=None, max_tokens=20000, on_token=None):
        return "Section text."

    captured = {}

    async def fake_resolve(sections, **kwargs):
        captured["model"] = kwargs.get("model")
        return sections, ""

    monkeypatch.setattr(mg, "_achat", fake_achat)
    monkeypatch.setattr(mg, "resolve_citations", fake_resolve)

    await mg.generate_manuscript_draft(
        pipeline_outputs={}, interview_data={}, pipeline_type="nanopore_mag",
        bioproject_accession="PRJNA000", base_url=NO_LLM,
        model="draft-model",  # cite_model unset
    )
    assert captured["model"] == "draft-model"
