"""Real-data fixtures for prompt/model benchmarking.

Derives a `parse_microscape`-shaped ``pipeline_outputs`` dict from the viz JSONs
of a real submission (c5af6277 — a sea-ice "frost flower" 16S amplicon run,
BioProject PRJNA1473294). This is the run whose subject matter earlier models
hallucinated (see manuscript_generator._format_study), so it doubles as an
anti-fabrication regression fixture.

The raw viz data lives OUTSIDE the repo (default /data/dev/testdata/c5af6277,
override with OMC_BENCH_DATA). `build_pipeline_outputs()` computes the summary;
`load_fixture()` prefers the committed snapshot so the harness runs without the
raw data present.
"""
import gzip
import json
import os
from collections import Counter
from pathlib import Path

DATA_DIR = Path(os.environ.get("OMC_BENCH_DATA", "/data/dev/testdata/c5af6277"))
SNAPSHOT = Path(__file__).parent / "fixture_c5af6277.json"


# ── Real, verified study metadata (from NCBI eutils, SRR38958117) ────────────
# Fetched, not invented. The grounded condition must stay truthful; the minimal
# and none conditions exist to test that models DON'T invent a study system.
STUDY_GROUNDED = {
    "title": "16S amplicon sequencing frost flower",
    "study_name": "Ice Chamber Experiment Metagenome",
    "organism": "aquatic metagenome",
    "organization": "Univ. Grenoble Alpes, Institut des Géosciences de l'Environnement",
    "platform": "Illumina MiSeq",
    "num_samples": 11,
    "num_sra_runs": 11,
    "bioproject": "PRJNA1473294",
    "description": (
        "16S rRNA gene amplicon sequencing of frost flowers and associated "
        "sea-ice brine from an ice chamber experiment, characterising the "
        "bacterial communities that concentrate at the sea-ice/atmosphere interface."
    ),
}

# Only what the platform knows before any interview: an accession and a count.
STUDY_MINIMAL = {
    "bioproject": "PRJNA1473294",
    "num_samples": 11,
    "num_sra_runs": 11,
}

STUDY_NONE = None


def _read_json(path: Path):
    """Read a viz JSON, or None if absent (submissions vary in which files they emit)."""
    for p in (path, path.with_suffix(path.suffix + ".gz")):
        if p.exists():
            opener = gzip.open if p.suffix == ".gz" else open
            with opener(p, "rt") as f:
                return json.load(f)
    return None


def build_pipeline_outputs(data_dir: Path = DATA_DIR) -> dict:
    """Compute a parse_microscape-shaped summary from whatever viz JSONs exist.

    Degrades gracefully like parse_microscape: any missing file just drops the
    fields it would have supplied, so submissions without provenance (retention)
    or without taxonomy still yield a usable summary."""
    data_dir = Path(data_dir)
    renorm = _read_json(data_dir / "renorm_stats.json") or {}
    samples = _read_json(data_dir / "samples.json") or []
    taxonomy = _read_json(data_dir / "taxonomy.json") or {}
    provenance = _read_json(data_dir / "provenance.json") or {}

    # Primary taxonomy DB (silva here); tolerate an absent/empty taxonomy file.
    db_name, tax = next(iter(taxonomy.items()), ("none", {}))
    levels = tax.get("levels", [])
    assignments = tax.get("assignments", {})

    unassigned = {"", "na", "unclassified", "unassigned", "incertae sedis", "none"}

    def _clean(v):
        return None if (v or "").strip().lower() in unassigned else v

    classified_per_rank = {}
    for i, level in enumerate(levels):
        classified_per_rank[level.lower()] = sum(
            1 for a in assignments.values() if i < len(a) and _clean(a[i]) is not None
        )
    phylum_idx = levels.index("Phylum") if "Phylum" in levels else 1
    phyla = Counter(
        v for a in assignments.values()
        if phylum_idx < len(a) and (v := _clean(a[phylum_idx])) is not None
    )
    top_phyla = dict(phyla.most_common(5))

    prok = renorm.get("prokaryote", {})
    total = provenance.get("total", {})
    reads_in = total.get("raw", 0)
    reads_out = total.get("filter", 0)

    return {
        "asv_summary": {
            # The WHOLE table, which is what `counts` holds. This reported the
            # prokaryote sub-table's dimensions (414 ASVs, 44 samples) while listing
            # all 63 sample ids beside them, so an analyst comparing it with a
            # 63 x 735 `counts` found a contradiction and started doubting the frame.
            "total_asvs": len(assignments),
            "n_samples": len(samples),
            # The per-domain split is real and stays available — correctly labelled.
            "by_category": {k: {"n_asvs": v.get("n_asvs"), "n_samples": v.get("n_samples"),
                                "n_reads": v.get("n_reads")}
                            for k, v in (renorm or {}).items() if isinstance(v, dict)},
            "samples": [s["id"] for s in samples],
        },
        "taxonomy_summary": {
            "database": db_name,
            "databases_used": [db_name],
            "total_asvs_classified": len(assignments),
            "classified_per_rank": classified_per_rank,
            "top_phyla": top_phyla,
            "phyla": top_phyla,
        },
        "filtering": {
            # Samples ATTEMPTED. Named apart from asv_summary.n_samples, which counts
            # the ones that reached the table; the bare name in both places put 84 and
            # 63 side by side with nothing to say they measure different things.
            "n_samples_attempted": len(provenance.get("samples", {})),
            "reads_input": reads_in,
            "reads_retained": reads_out,
            "retention_pct": round(100 * reads_out / reads_in, 1) if reads_in else 0,
        },
        "renorm": {
            "prokaryote": prok,
            "chloroplast": renorm.get("chloroplast", {}),
        },
        "result_categories": ["taxonomy", "seqtab_final", "filtered", "renormalized"],
    }


def load_fixture() -> dict:
    """Return pipeline_outputs, preferring the committed snapshot.

    Falls back to computing from raw data if the snapshot is missing.
    """
    if SNAPSHOT.exists():
        return json.loads(SNAPSHOT.read_text())
    return build_pipeline_outputs()


def refresh_snapshot() -> dict:
    """Recompute from raw data and write the committed snapshot."""
    outputs = build_pipeline_outputs()
    SNAPSHOT.write_text(json.dumps(outputs, indent=2) + "\n")
    return outputs


PIPELINE_TYPE = "microscape"
BIOPROJECT = "PRJNA1473294"


if __name__ == "__main__":
    out = refresh_snapshot()
    print(f"Wrote {SNAPSHOT}")
    print(json.dumps(out, indent=2))
