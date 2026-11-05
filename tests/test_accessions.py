"""Unit tests for the accession box parser (lists and ranges)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "portal"))

from app.accessions import AccessionError, kind_of, parse_accessions, summarize  # noqa: E402


def test_single():
    assert parse_accessions(" prjna656268 ") == ["PRJNA656268"]


def test_space_and_comma_separators():
    assert parse_accessions("SRR1 SRR2, SRR3,SRR4;SRR5\nSRR6") == [
        "SRR1", "SRR2", "SRR3", "SRR4", "SRR5", "SRR6"]


def test_dedup_keeps_order():
    assert parse_accessions("SRR2 SRR1 SRR2") == ["SRR2", "SRR1"]


def test_full_range_with_dash_and_dots():
    assert parse_accessions("SRR100-SRR103") == ["SRR100", "SRR101", "SRR102", "SRR103"]
    assert parse_accessions("SRR100..SRR101") == ["SRR100", "SRR101"]
    assert parse_accessions("SRR100 – SRR101") == ["SRR100", "SRR101"]


def test_short_range_keeps_prefix_digits():
    assert parse_accessions("SRR12345670-72") == ["SRR12345670", "SRR12345671", "SRR12345672"]


def test_range_preserves_zero_padding():
    assert parse_accessions("SAMN0000098-SAMN0000101") == [
        "SAMN0000098", "SAMN0000099", "SAMN0000100", "SAMN0000101"]


def test_mixed_list_and_ranges():
    assert parse_accessions("PRJEB1, SAMN5-SAMN6 SRR9") == ["PRJEB1", "SAMN5", "SAMN6", "SRR9"]


@pytest.mark.parametrize("bad", [
    "", "   ", "hello", "SRR1-ERR2", "SRR5-SRR2", "SRR1-SRR2-SRR3", "12345", "SRRabc",
    "SRR1-SRR9999999",
])
def test_rejects(bad):
    with pytest.raises(AccessionError):
        parse_accessions(bad)


def test_kinds():
    assert kind_of("PRJNA1") == "project"
    assert kind_of("SAMEA1") == "biosample"
    assert kind_of("SAME1") == "biosample"
    assert kind_of("ERS1") == "sample"
    assert kind_of("SRX1") == "experiment"
    assert kind_of("DRR1") == "run"
    assert kind_of("SRP1") == "study"
    assert kind_of("XYZ1") is None


def test_summarize():
    assert summarize(["SRR1"]) == "SRR1"
    s = summarize([f"SRR{n}" for n in range(1, 40)])
    assert s.endswith("more)") and len(s) <= 50


# ── merge_project_metadata ──────────────────────────────────────────────────
from app.sra_metadata import merge_project_metadata  # noqa: E402


def _proj(acc, runs, strategy="AMPLICON", organism="soil metagenome"):
    return {
        "accession": acc, "title": f"Title {acc}", "description": f"Desc {acc}",
        "organism": organism, "organization": "Org", "registration_date": "2020-01-01",
        "num_sra_runs": len(runs), "num_samples": len(runs), "total_bases": 10 * len(runs),
        "total_spots": len(runs),
        "breakdown": [{"platform": "ILLUMINA", "instrument": "MiSeq", "strategy": strategy,
                       "source": "METAGENOMIC", "layout": "PAIRED", "runs": len(runs),
                       "samples": len(runs), "bases": 10 * len(runs), "spots": len(runs),
                       "run_accessions": list(runs)}],
        "sample_records": [{"run_accession": r, "study_accession": acc} for r in runs],
    }


def test_merge_single_passthrough():
    m = merge_project_metadata([_proj("PRJNA1", ["SRR1", "SRR2"])])
    assert m["accession"] == "PRJNA1" and m["accessions"] == ["PRJNA1"]
    assert m["title"] == "Title PRJNA1" and len(m["projects"]) == 1


def test_merge_two_projects_sums_breakdown_and_records():
    m = merge_project_metadata([_proj("PRJNA1", ["SRR1", "SRR2"]),
                                _proj("PRJEB2", ["ERR1"], organism="marine metagenome")])
    assert m["accessions"] == ["PRJNA1", "PRJEB2"]
    assert m["num_sra_runs"] == 3 and m["total_bases"] == 30
    assert len(m["breakdown"]) == 1
    assert m["breakdown"][0]["runs"] == 3
    assert m["breakdown"][0]["run_accessions"] == ["SRR1", "SRR2", "ERR1"]
    assert [r["run_accession"] for r in m["sample_records"]] == ["SRR1", "SRR2", "ERR1"]
    assert m["organism"] == "soil metagenome, marine metagenome"
    assert m["title"] == "Title PRJNA1; Title PRJEB2"
    assert "[PRJEB2] Desc PRJEB2" in m["description"]


def test_merge_keeps_distinct_breakdown_rows():
    m = merge_project_metadata([_proj("PRJNA1", ["SRR1"]), _proj("PRJEB2", ["ERR1"], strategy="WGS")])
    assert {b["strategy"] for b in m["breakdown"]} == {"AMPLICON", "WGS"}
