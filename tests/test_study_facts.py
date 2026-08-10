"""One study-facts builder feeding every AI consumer (issue #56).

The bug this guards against is silent: three paths each assembled study context
independently, so a fact added in one place simply never reached the other
models — no error, just a less-informed prompt.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portal.app.study_facts import build_study_facts, _amplicon_design
from ai.manuscript_generator import _format_study


class _Sub:
    """Minimal stand-in — build_study_facts only reads attributes."""
    def __init__(self, **kw):
        self.sample_metadata = kw.get("sample_metadata")
        self.bioproject_accession = kw.get("bioproject_accession")
        self.pipeline = kw.get("pipeline")
        self.primers = kw.get("primers")


class _Pipeline:
    def __init__(self, value): self.value = value


META = {"title": "Frost flower amplicons", "organism": "sea ice",
        "num_samples": 84, "description": "16S amplicon sequencing"}

PRIMERS = {
    "source": "inferred-db", "fwd_name": "341F", "rev_name": "Bakt_805R",
    "region": "16S V3-V4", "runs": ["SRR1", "SRR2"],
    "sets": [
        {"fwd_name": "341F", "rev_name": "Bakt_805R", "region": "16S V3-V4", "n_runs": 44},
        {"fwd_name": "A-528F", "rev_name": "B-706R", "region": "18S V4", "n_runs": 40},
    ],
}


# ── the builder ───────────────────────────────────────────────────────────────

def test_facts_are_a_superset_of_sample_metadata():
    # Existing consumers reach for facts["title"] etc., so new facts must be
    # purely additive rather than a reshaped payload.
    facts = build_study_facts(_Sub(sample_metadata=META))
    for k, v in META.items():
        assert facts[k] == v


def test_accession_and_pipeline_are_included():
    facts = build_study_facts(_Sub(
        sample_metadata=META, bioproject_accession="PRJNA1473294",
        pipeline=_Pipeline("MICROSCAPE")))
    assert facts["accession"] == "PRJNA1473294"
    assert facts["pipeline"] == "MICROSCAPE"


def test_absent_facts_are_omitted_not_nulled():
    # A key present with a null value reads to a model as "measured, found none".
    facts = build_study_facts(_Sub())
    assert "amplicon" not in facts
    assert "accession" not in facts
    assert "primers" not in facts


def test_sample_metadata_accession_is_not_overwritten():
    facts = build_study_facts(_Sub(
        sample_metadata={"accession": "FROM-METADATA"},
        bioproject_accession="FROM-COLUMN"))
    assert facts["accession"] == "FROM-METADATA"


def test_amplicon_design_summarises_every_set():
    d = _amplicon_design(PRIMERS)
    assert [x["region"] for x in d["designs"]] == ["16S V3-V4", "18S V4"]
    assert d["designs"][0]["forward"] == "341F"


def test_amplicon_design_is_none_without_primers():
    assert _amplicon_design(None) is None
    assert _amplicon_design({}) is None


# ── it actually reaches the manuscript prompt ─────────────────────────────────

def test_manuscript_prompt_still_leads_with_the_named_fields():
    text = _format_study(build_study_facts(_Sub(sample_metadata=META)))
    assert "Title: Frost flower amplicons" in text
    assert "Organism: sea ice" in text


def test_manuscript_prompt_gains_the_amplicon_design():
    text = _format_study(build_study_facts(_Sub(sample_metadata=META, primers=PRIMERS)))
    assert "Amplicon design:" in text
    assert "341F" in text and "A-528F" in text
    # facts only — the study block states what the assay is, it does not carry
    # instructions to the model
    assert "do not assert" not in text


def test_manuscript_prompt_unchanged_when_there_are_no_primers():
    text = _format_study(build_study_facts(_Sub(sample_metadata=META)))
    assert "Amplicon design" not in text


def test_no_metadata_still_warns_against_inventing_a_study():
    # The regression this guards: a sea-ice run was once written up as a
    # forensic "thanatomicrobiome" study.
    text = _format_study(build_study_facts(_Sub()))
    assert "do NOT guess the subject matter" in text


# ── prompts have a size budget ────────────────────────────────────────────────
#
# Review json.dumps these facts straight into its prompt. sample_metadata also
# carries bulk per-sample payloads, so passing them through turned a
# 62-character config into ~1.8M tokens on real submissions.

def test_bulk_per_sample_containers_are_summarised_not_inlined():
    import json
    big = [{"run": f"SRR{i}", "blurb": "x" * 200} for i in range(200)]
    facts = build_study_facts(_Sub(sample_metadata={"title": "T", "sample_records": big}))

    assert facts["sample_records"]["n"] == 200
    assert "too large for a prompt" in facts["sample_records"]["omitted"]
    assert len(json.dumps(facts, default=str)) < 2000, "facts blob must stay prompt-sized"


def test_small_containers_are_kept_intact():
    facts = build_study_facts(_Sub(sample_metadata={"breakdown": {"16S": 44, "18S": 40}}))
    assert facts["breakdown"] == {"16S": 44, "18S": 40}


def test_long_strings_are_not_truncated():
    # A study abstract is the subject matter the model needs, and unlike a
    # per-sample collection it doesn't grow with sample count.
    abstract = "Frost flowers " * 500
    facts = build_study_facts(_Sub(sample_metadata={"description": abstract}))
    assert facts["description"] == abstract
