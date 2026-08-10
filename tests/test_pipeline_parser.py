"""Test pipeline output parser against a real nanopore-MAG results tree.

These assert on a specific local dataset rather than a checked-in fixture, so
they only run where that dataset exists. Point OMC_TEST_RESULTS at an equivalent
results directory to run them elsewhere; otherwise the module skips (previously
it failed with KeyError on every parser, which read as broken code rather than
absent data).
"""
import os
import sys
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

RESULTS = Path(os.environ.get(
    "OMC_TEST_RESULTS",
    "/data/project_cyanomag/results/nanopore_mag_microcystis",
))

pytestmark = pytest.mark.skipif(
    not RESULTS.is_dir(),
    reason=f"pipeline results dataset not present at {RESULTS} "
           "(set OMC_TEST_RESULTS to a nanopore-MAG results dir to run these)",
)


def test_parse_checkm2():
    from ai.pipeline_parser import parse_checkm2
    r = parse_checkm2(RESULTS)
    assert r["total_bins"] > 100
    assert 0 < r["completeness_mean"] < 100
    assert r["contamination_mean"] >= 0
    print(f"CheckM2: {r['total_bins']} bins, {r['high_quality']} HQ, {r['medium_quality']} MQ")


def test_parse_dastool():
    from ai.pipeline_parser import parse_dastool
    r = parse_dastool(RESULTS)
    assert r["consensus_bins"] > 0
    assert all(b["completeness"] >= 0 for b in r["bins"])
    print(f"DAS Tool: {r['consensus_bins']} consensus bins")


def test_parse_gtdbtk():
    from ai.pipeline_parser import parse_gtdbtk
    r = parse_gtdbtk(RESULTS)
    assert r["total_classified"] > 100
    assert "Cyanobacteriota" in r["phyla"]
    print(f"GTDB-Tk: {r['total_classified']} classified, {len(r['phyla'])} phyla")


def test_parse_assembly():
    from ai.pipeline_parser import parse_assembly
    r = parse_assembly(RESULTS)
    assert r["total_contigs"] > 0
    assert r["total_length_mb"] > 0
    assert r["n50_kb"] > 0
    print(f"Assembly: {r['total_contigs']} contigs, {r['total_length_mb']} Mb, N50={r['n50_kb']} kb")


def test_parse_metabolism():
    from ai.pipeline_parser import parse_metabolism
    r = parse_metabolism(RESULTS)
    assert r["total_pathways_tested"] > 0
    assert r["active_pathways"] > 0
    print(f"Metabolism: {r['active_pathways']}/{r['total_pathways_tested']} active pathways")


def test_parse_mge():
    from ai.pipeline_parser import parse_mge
    r = parse_mge(RESULTS)
    # At least some MGE category should be present
    assert len(r) > 0
    print(f"MGE: {r}")


def test_parse_full_nanopore_mag():
    """Parse all outputs from a complete nanopore_mag run."""
    from ai.pipeline_parser import parse_nanopore_mag
    r = parse_nanopore_mag(RESULTS)
    assert "MAG_summary" in r
    assert "consensus_binning" in r
    assert "taxonomy_summary" in r
    assert "assembly_stats" in r
    assert "metabolism" in r
    assert "binners_used" in r
    print(f"\nFull parse: {len(r)} categories")
    for key, val in r.items():
        if isinstance(val, dict):
            print(f"  {key}: {len(val)} fields")
        elif isinstance(val, list):
            print(f"  {key}: {len(val)} items")
        else:
            print(f"  {key}: {val}")
