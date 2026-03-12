"""End-to-end test with REAL pipeline outputs from /data/project_cyanomag.

Parses actual CheckM2, GTDB-Tk, and DAS Tool results from a completed
nanopore_mag run and feeds them to the manuscript generator + review agents.
"""
import csv
import sys
import json
import pytest
from pathlib import Path

sys.path.insert(0, "/data/omc/omc-platform")

RESULTS = Path("/data/project_cyanomag/results/nanopore_mag_microcystis")


def parse_checkm2(path: Path) -> dict:
    """Parse CheckM2 quality_report.tsv into summary stats."""
    rows = list(csv.DictReader(open(path), delimiter="\t"))
    completeness = [float(r["Completeness"]) for r in rows]
    contamination = [float(r["Contamination"]) for r in rows]
    sizes = [int(r["Genome_Size"]) for r in rows]
    hq = [r for r in rows if float(r["Completeness"]) >= 50 and float(r["Contamination"]) < 10]
    return {
        "total_bins": len(rows),
        "high_quality": len([r for r in rows if float(r["Completeness"]) >= 90 and float(r["Contamination"]) < 5]),
        "medium_quality": len([r for r in rows if 50 <= float(r["Completeness"]) < 90 and float(r["Contamination"]) < 10]),
        "completeness_mean": round(sum(completeness) / len(completeness), 1),
        "contamination_mean": round(sum(contamination) / len(contamination), 1),
        "genome_size_mean_mb": round(sum(sizes) / len(sizes) / 1e6, 2),
        "passing_bins": len(hq),
    }


def parse_gtdbtk(path: Path) -> list[dict]:
    """Parse GTDB-Tk taxonomy into bin→classification mapping."""
    rows = list(csv.DictReader(open(path), delimiter="\t"))
    results = []
    for r in rows:
        tax = r["classification"]
        parts = {t.split("__")[0]: t.split("__")[1] for t in tax.split(";") if "__" in t and t.split("__")[1]}
        results.append({
            "bin": r["user_genome"],
            "classification": tax,
            "domain": parts.get("d", ""),
            "phylum": parts.get("p", ""),
            "genus": parts.get("g", ""),
            "species": parts.get("s", ""),
        })
    return results


def parse_dastool(path: Path) -> dict:
    """Parse DAS Tool summary."""
    rows = list(csv.DictReader(open(path), delimiter="\t"))
    return {
        "consensus_bins": len(rows),
        "bins": [{"bin": r["bin"], "completeness": float(r["SCG_completeness"]),
                  "redundancy": float(r["SCG_redundancy"]), "contigs": int(r["contigs"]),
                  "size_mb": round(int(r["size"]) / 1e6, 2)} for r in rows],
    }


@pytest.fixture
def pipeline_outputs():
    """Parse real pipeline outputs into manuscript-generator format."""
    checkm2 = parse_checkm2(RESULTS / "binning/checkm2/quality_report.tsv")
    gtdbtk = parse_gtdbtk(RESULTS / "taxonomy/gtdbtk/gtdbtk_taxonomy.tsv")
    dastool = parse_dastool(RESULTS / "binning/dastool/summary.tsv")

    # Summarize taxonomy
    phyla = {}
    genera = {}
    for t in gtdbtk:
        if t["phylum"]:
            phyla[t["phylum"]] = phyla.get(t["phylum"], 0) + 1
        if t["genus"]:
            genera[t["genus"]] = genera.get(t["genus"], 0) + 1

    return {
        "MAG_summary": checkm2,
        "consensus_binning": dastool,
        "taxonomy_summary": {
            "method": "GTDB-Tk v2",
            "total_classified": len(gtdbtk),
            "phyla": dict(sorted(phyla.items(), key=lambda x: -x[1])),
            "genera": dict(sorted(genera.items(), key=lambda x: -x[1])[:10]),
        },
    }


@pytest.fixture
def interview_data():
    """Realistic interview answers for cyanobacterial MAG study."""
    return {
        "research_question": "We're characterizing the cyanobacterial community in a freshwater bloom using nanopore long-read metagenomics, with focus on Microcystis and toxin-producing genotypes.",
        "study_context": "Samples from a harmful algal bloom in a Canadian freshwater lake. Nanopore sequencing on MinION for real-time monitoring potential.",
        "sample_info": "Single bloom sample, size-fractionated to enrich for cyanobacteria. Oxford Nanopore MinION, R10.4.1 chemistry.",
        "expected_findings": "Dominant Microcystis aeruginosa with co-occurring Planktothrix and Dolichospermum. Expect microcystin biosynthesis genes (mcy cluster) in multiple MAGs.",
        "broader_significance": "Long-read metagenomics enables near-complete MAG recovery from complex blooms, improving strain-level resolution for toxin risk assessment.",
        "limitations": "Single sample, no temporal replication. Nanopore error rates may affect fine-scale variant calling. Limited eukaryotic recovery.",
        "additional_context": "Part of a monitoring program for cyanotoxins in drinking water sources.",
    }


def test_parse_real_checkm2(pipeline_outputs):
    """Verify we can parse real CheckM2 output."""
    s = pipeline_outputs["MAG_summary"]
    assert s["total_bins"] > 0
    assert s["completeness_mean"] > 0
    print(f"\nCheckM2: {s['total_bins']} bins, {s['high_quality']} HQ, {s['medium_quality']} MQ")
    print(f"  Mean completeness: {s['completeness_mean']}%, mean contamination: {s['contamination_mean']}%")


def test_parse_real_gtdbtk(pipeline_outputs):
    """Verify we can parse real GTDB-Tk taxonomy."""
    tax = pipeline_outputs["taxonomy_summary"]
    assert tax["total_classified"] > 0
    assert "Cyanobacteriota" in tax["phyla"]
    print(f"\nGTDB-Tk: {tax['total_classified']} classified")
    print(f"  Phyla: {tax['phyla']}")
    print(f"  Top genera: {tax['genera']}")


def test_parse_real_dastool(pipeline_outputs):
    """Verify we can parse real DAS Tool consensus."""
    d = pipeline_outputs["consensus_binning"]
    assert d["consensus_bins"] > 0
    print(f"\nDAS Tool: {d['consensus_bins']} consensus bins")
    for b in d["bins"][:3]:
        print(f"  {b['bin']}: {b['completeness']}% complete, {b['size_mb']} MB")


@pytest.mark.asyncio
@pytest.mark.timeout(600)
async def test_manuscript_from_real_data(pipeline_outputs, interview_data):
    """Generate manuscript sections from real pipeline outputs."""
    from ai.manuscript_generator import generate_manuscript_draft

    sections = await generate_manuscript_draft(
        pipeline_outputs,
        interview_data,
        "nanopore_mag",
        "LOCAL_CYANO_BLOOM",
    )

    for sec_name in ["introduction", "methods", "results", "discussion", "abstract"]:
        assert sec_name in sections, f"Missing section: {sec_name}"
        content = sections[sec_name]
        assert len(content) > 50, f"Section '{sec_name}' too short ({len(content)} chars)"
        print(f"\n{'='*60}")
        print(f"  {sec_name.upper()} ({len(content)} chars)")
        print(f"{'='*60}")
        print(content[:500])
        if len(content) > 500:
            print(f"  ... [{len(content) - 500} more chars]")


@pytest.mark.asyncio
@pytest.mark.timeout(600)
async def test_review_real_manuscript(pipeline_outputs, interview_data):
    """Run review agents on manuscript generated from real data."""
    from ai.manuscript_generator import generate_manuscript_draft
    from ai.review_agents import run_all_reviews

    # Generate first
    manuscript = await generate_manuscript_draft(
        pipeline_outputs, interview_data, "nanopore_mag", "LOCAL_CYANO_BLOOM",
    )

    # Review
    reviews = await run_all_reviews(
        manuscript, pipeline_outputs,
        {"pipeline": "nanopore_mag", "assembler": "Flye", "binners": "7 algorithms + DAS Tool consensus"},
    )

    assert len(reviews) == 3
    for r in reviews:
        print(f"\n--- {r['review_type'].upper()} REVIEW ---")
        print(f"  {len(r['comments'])} comments")
        if r.get("summary"):
            print(f"  Summary: {r['summary'][:200]}")
        for c in r["comments"][:2]:
            sev = c.get("severity", "?")
            conf = c.get("confidence", "?")
            print(f"  [{sev}, conf={conf}] {c.get('issue', c.get('detail', ''))[:100]}")
