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
