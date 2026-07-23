"""Tests for deterministic checks + the revise loop (issue #20).

The check tests are pure. The revise tests are made deterministic either by
monkeypatching the LLM call with a canned rewrite, or by pointing it at a
fast-fail address to exercise graceful degradation — no live LLM/network.
"""
import sys
import pytest

sys.path.insert(0, "/data/omc/omc-platform")

NO_LLM = "http://127.0.0.1:9/v1"

_LONG = "x" * 150  # padding to clear the thin-section (<100 char) check


# ---------------------------------------------------------------------------
# Deterministic checks
# ---------------------------------------------------------------------------

def test_check_placeholders_finds_and_grades():
    from ai.manuscript_checks import check_placeholders
    sections = {
        "introduction": "A claim [CITE]. Another [CITE: Smith 2020]. A gap [AUTHOR: describe site].",
        "methods": "We used tools [CITATION NEEDED] and [TODO fill in].",
    }
    issues = check_placeholders(sections)
    kinds = {(i["section"], i["severity"]) for i in issues}
    # [CITE] variants are "major"; [AUTHOR:]/[CITATION NEEDED]/[TODO] are "critical"
    assert ("introduction", "major") in kinds
    assert ("introduction", "critical") in kinds
    assert ("methods", "critical") in kinds
    # [CITE] and [CITE: ...] dedupe to one token form each within a section
    intro_cite = [i for i in issues if i["section"] == "introduction" and i["severity"] == "major"]
    assert len(intro_cite) == 2  # "[CITE]" and "[CITE: Smith 2020]" are distinct tokens


def test_check_required_sections():
    from ai.manuscript_checks import check_required_sections
    sections = {
        "abstract": _LONG, "introduction": _LONG, "methods": _LONG,
        "results": "too short", "discussion": "",
    }
    issues = check_required_sections(sections)
    by = {i["section"]: i["issue"] for i in issues}
    assert by["results"] == "thin-section"
    assert by["discussion"] == "missing-section"
    assert "abstract" not in by


def test_check_figure_references():
    from ai.manuscript_checks import check_figure_references
    # Text references Figure 3 but only 2 figures exist → mismatch
    over = check_figure_references({"results": "See Figure 1 and Figure 3."},
                                   available_figures=["a", "b"])
    assert any(i["issue"] == "figure-reference-mismatch" for i in over)
    # Figures exist but none referenced → unused
    unused = check_figure_references({"results": "No figures mentioned here."},
                                     available_figures=["a", "b"])
    assert any(i["issue"] == "unused-figures" for i in unused)
    # No available figures → no issues
    assert check_figure_references({"results": "See Figure 9."}, available_figures=[]) == []


def test_check_numbers_supported():
    from ai.manuscript_checks import check_numbers_supported
    data = {"completeness": 98.6, "bins": 12, "coverage_pct": 45}
    sections = {
        "results": "Recovered a MAG at 98.6% completeness and 3.2% contamination in 2021.",
        "abstract": "We report 45% coverage.",
    }
    issues = check_numbers_supported(sections, data)
    # 98.6 is in the data (supported); 3.2 is not (flag); 2021 is a year (ignored)
    results_issue = [i for i in issues if i["section"] == "results"]
    assert results_issue and "3.2%" in results_issue[0]["detail"]
    assert "98.6" not in results_issue[0]["detail"]
    # 45% appears in the data as 45 → abstract is clean
    assert not any(i["section"] == "abstract" for i in issues)
    # No data → no checks
    assert check_numbers_supported(sections, None) == []


def test_run_all_checks_combines():
    from ai.manuscript_checks import run_all_checks
    sections = {"introduction": f"Intro [CITE]. {_LONG}"}
    issues = run_all_checks(sections, results_data=None, available_figures=None)
    kinds = {i["issue"] for i in issues}
    assert "unresolved-placeholder" in kinds
    assert "missing-section" in kinds  # abstract/methods/results/discussion absent


# ---------------------------------------------------------------------------
# flatten_review_comments
# ---------------------------------------------------------------------------

def test_flatten_review_comments():
    from ai.manuscript_generator import flatten_review_comments
    reviews = [{
        "review_type": "clarity",
        "comments": [{"section": "Results", "issue": "vague", "detail": "d",
                      "severity": "minor", "confidence": 0.4}],
        "summary": "s",
    }]
    flat = flatten_review_comments(reviews)
    assert flat[0]["section"] == "results"   # lowercased
    assert flat[0]["source"] == "clarity"
    assert len(flat) == 1


# ---------------------------------------------------------------------------
# revise_manuscript
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_revise_no_issues_is_noop():
    from ai.manuscript_generator import revise_manuscript
    sections = {s: _LONG for s in
                ["abstract", "introduction", "methods", "results", "discussion"]}
    revised, log = await revise_manuscript(sections, reviews=None, base_url=NO_LLM)
    assert revised == sections
    assert log == []


@pytest.mark.asyncio
async def test_revise_applies_rewrite(monkeypatch):
    import ai.manuscript_generator as mg

    async def fake_achat(client, system, user, model=None, max_tokens=8000):
        return "Clean introduction text with no placeholders whatsoever. " + _LONG

    monkeypatch.setattr(mg, "_achat", fake_achat)
    sections = {"introduction": f"Intro with a leftover [AUTHOR: describe study] note. {_LONG}"}
    revised, log = await mg.revise_manuscript(sections, base_url=NO_LLM, max_passes=2)

    assert "[AUTHOR" not in revised["introduction"]
    intro_log = [e for e in log if e["section"] == "introduction"]
    assert intro_log and intro_log[0]["changed"] is True
    assert intro_log[0]["passes"] == 1  # early-stop: placeholder cleared after one pass


@pytest.mark.asyncio
async def test_revise_strips_code_fences(monkeypatch):
    import ai.manuscript_generator as mg

    async def fake_achat(client, system, user, model=None, max_tokens=8000):
        return "```markdown\nFenced revised methods content. " + _LONG + "\n```"

    monkeypatch.setattr(mg, "_achat", fake_achat)
    sections = {"methods": f"Methods with [TODO expand]. {_LONG}"}
    revised, _ = await mg.revise_manuscript(sections, base_url=NO_LLM)
    assert not revised["methods"].startswith("```")
    assert "Fenced revised methods content" in revised["methods"]


@pytest.mark.asyncio
async def test_revise_degrades_without_llm():
    from ai.manuscript_generator import revise_manuscript
    sections = {"introduction": f"Intro with [AUTHOR: gap]. {_LONG}"}
    original = dict(sections)
    revised, log = await revise_manuscript(sections, base_url=NO_LLM)
    # LLM unreachable → section unchanged, error recorded, input not mutated
    assert revised["introduction"] == original["introduction"]
    assert sections == original
    intro_log = [e for e in log if e["section"] == "introduction"]
    assert intro_log and "error" in intro_log[0]
