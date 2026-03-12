"""Parse Nextflow pipeline outputs into structured data for manuscript generation.

Supports nanopore_mag pipeline outputs:
- Assembly stats (Flye)
- Binning (CheckM2, DAS Tool, individual binners)
- Taxonomy (GTDB-Tk, Kraken2)
- Metabolism (MinPath)
- MGE detection (DefenseFinder, integrons, genomic islands)
- Annotation (Bakta)
- Pipeline timing
"""
import csv
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def _read_tsv(path: Path, **kwargs) -> list[dict]:
    """Read a TSV file into list of dicts. Returns [] if file missing."""
    if not path.exists():
        log.debug(f"File not found: {path}")
        return []
    try:
        with open(path) as f:
            lines = list(f)
        # Keep first line starting with # if it looks like a header (has tab-separated column names)
        # Strip # prefix from header line
        if lines and lines[0].startswith("#"):
            lines[0] = lines[0].lstrip("#")
        # Skip remaining comment lines
        lines = [lines[0]] + [l for l in lines[1:] if not l.startswith("#")]
        return list(csv.DictReader(lines, delimiter="\t", **kwargs))
    except Exception as e:
        log.warning(f"Failed to parse {path}: {e}")
        return []


def parse_checkm2(results_dir: Path) -> dict:
    """Parse CheckM2 quality_report.tsv."""
    rows = _read_tsv(results_dir / "binning/checkm2/quality_report.tsv")
    if not rows:
        return {}
    completeness = [float(r["Completeness"]) for r in rows]
    contamination = [float(r["Contamination"]) for r in rows]
    sizes = [int(r["Genome_Size"]) for r in rows]
    return {
        "total_bins": len(rows),
        "high_quality": len([r for r in rows if float(r["Completeness"]) >= 90 and float(r["Contamination"]) < 5]),
        "medium_quality": len([r for r in rows if 50 <= float(r["Completeness"]) < 90 and float(r["Contamination"]) < 10]),
        "low_quality": len([r for r in rows if float(r["Completeness"]) < 50]),
        "completeness_mean": round(sum(completeness) / len(completeness), 1),
        "completeness_max": round(max(completeness), 1),
        "contamination_mean": round(sum(contamination) / len(contamination), 1),
        "genome_size_mean_mb": round(sum(sizes) / len(sizes) / 1e6, 2),
    }


def parse_dastool(results_dir: Path) -> dict:
    """Parse DAS Tool consensus binning summary."""
    rows = _read_tsv(results_dir / "binning/dastool/summary.tsv")
    if not rows:
        return {}
    return {
        "consensus_bins": len(rows),
        "bins": [{
            "bin": r["bin"],
            "completeness": float(r["SCG_completeness"]),
            "redundancy": float(r["SCG_redundancy"]),
            "contigs": int(r["contigs"]),
            "size_mb": round(int(r["size"]) / 1e6, 2),
        } for r in rows],
    }


def parse_gtdbtk(results_dir: Path) -> dict:
    """Parse GTDB-Tk taxonomy classification."""
    rows = _read_tsv(results_dir / "taxonomy/gtdbtk/gtdbtk_taxonomy.tsv")
    if not rows:
        return {}
    phyla = {}
    genera = {}
    for r in rows:
        tax = r.get("classification", "")
        parts = {t.split("__")[0]: t.split("__")[1] for t in tax.split(";") if "__" in t and t.split("__")[1]}
        p = parts.get("p", "")
        g = parts.get("g", "")
        if p:
            phyla[p] = phyla.get(p, 0) + 1
        if g:
            genera[g] = genera.get(g, 0) + 1
    return {
        "method": "GTDB-Tk v2",
        "total_classified": len(rows),
        "phyla": dict(sorted(phyla.items(), key=lambda x: -x[1])),
        "genera": dict(sorted(genera.items(), key=lambda x: -x[1])[:15]),
    }


def parse_assembly(results_dir: Path) -> dict:
    """Parse Flye assembly stats."""
    info_path = results_dir / "assembly/flye_assemble/flye_out/assembly_info.txt"
    rows = _read_tsv(info_path)
    if not rows:
        return {}
    lengths = [int(r.get("length", 0)) for r in rows]
    coverages = [float(r.get("cov.", 0)) for r in rows]
    circular = sum(1 for r in rows if r.get("circ.", "N") == "Y")
    return {
        "total_contigs": len(rows),
        "total_length_mb": round(sum(lengths) / 1e6, 2),
        "n50_kb": round(_calc_n50(lengths) / 1e3, 1),
        "largest_contig_kb": round(max(lengths) / 1e3, 1) if lengths else 0,
        "mean_coverage": round(sum(coverages) / len(coverages), 1) if coverages else 0,
        "circular_contigs": circular,
    }


def parse_metabolism(results_dir: Path) -> dict:
    """Parse MinPath metabolic pathway predictions."""
    rows = _read_tsv(results_dir / "metabolism/minpath/minpath_pathways.tsv")
    if not rows:
        return {}
    # Community-level pathways
    community = [r for r in rows if r.get("mag_id") == "_community"]
    active = [r for r in community if r.get("minpath") == "1"]
    return {
        "total_pathways_tested": len(community),
        "active_pathways": len(active),
        "top_pathways": [{"id": r["pathway_id"], "name": r["pathway_name"],
                          "families_found": int(r["found_families"]),
                          "families_total": int(r["total_families"])}
                         for r in active[:10]],
    }


def parse_mge(results_dir: Path) -> dict:
    """Parse mobile genetic element detection results."""
    mge_dir = results_dir / "mge"
    result = {}

    # Defense systems
    defense = _read_tsv(mge_dir / "defensefinder/systems.tsv")
    if defense:
        types = {}
        for r in defense:
            t = r.get("type", "unknown")
            types[t] = types.get(t, 0) + 1
        result["defense_systems"] = {"total": len(defense), "types": types}

    # Integrons
    integrons = _read_tsv(mge_dir / "integrons/summary.tsv")
    if integrons:
        result["integrons"] = len(integrons)

    # Genomic islands
    islands = _read_tsv(mge_dir / "islandpath/genomic_islands.tsv")
    if islands:
        result["genomic_islands"] = len(islands)

    return result


def parse_pipeline_timing(results_dir: Path) -> dict:
    """Parse pipeline timing information."""
    path = results_dir / "pipeline_info/pipeline_timing.tsv"
    if not path.exists():
        return {}
    rows = _read_tsv(path, fieldnames=["step", "event", "timestamp", "exit_code"])
    steps = {}
    for r in rows:
        step = r.get("step", "")
        event = r.get("event", "")
        ts = r.get("timestamp", "")
        if step and event and ts:
            if step not in steps:
                steps[step] = {}
            steps[step][event.lower()] = ts
    return {"steps": len(steps), "step_names": list(steps.keys())}


def parse_nanopore_mag(results_dir: Path) -> dict:
    """Parse all outputs from a nanopore_mag pipeline run.

    Returns a structured dict suitable for manuscript generation.
    """
    results_dir = Path(results_dir)
    if not results_dir.exists():
        log.warning(f"Results directory not found: {results_dir}")
        return {}

    outputs = {}

    # Core analyses
    checkm2 = parse_checkm2(results_dir)
    if checkm2:
        outputs["MAG_summary"] = checkm2

    dastool = parse_dastool(results_dir)
    if dastool:
        outputs["consensus_binning"] = dastool

    gtdbtk = parse_gtdbtk(results_dir)
    if gtdbtk:
        outputs["taxonomy_summary"] = gtdbtk

    assembly = parse_assembly(results_dir)
    if assembly:
        outputs["assembly_stats"] = assembly

    metabolism = parse_metabolism(results_dir)
    if metabolism:
        outputs["metabolism"] = metabolism

    mge = parse_mge(results_dir)
    if mge:
        outputs["mobile_genetic_elements"] = mge

    timing = parse_pipeline_timing(results_dir)
    if timing:
        outputs["pipeline_info"] = timing

    # Count individual binners used
    binner_dirs = [d.name for d in (results_dir / "binning").iterdir()
                   if d.is_dir() and d.name not in ("checkm2", "dastool")]
    if binner_dirs:
        outputs["binners_used"] = binner_dirs

    log.info(f"Parsed {len(outputs)} output categories from {results_dir}")
    return outputs


def _calc_n50(lengths: list[int]) -> int:
    """Calculate N50 from a list of contig lengths."""
    if not lengths:
        return 0
    sorted_lengths = sorted(lengths, reverse=True)
    total = sum(sorted_lengths)
    running = 0
    for l in sorted_lengths:
        running += l
        if running >= total / 2:
            return l
    return sorted_lengths[-1]


# Pipeline parser registry
PARSERS = {
    "nanopore_mag": parse_nanopore_mag,
}


def parse_pipeline_outputs(pipeline_type: str, results_dir: str | Path) -> dict:
    """Parse outputs for any supported pipeline type."""
    parser = PARSERS.get(pipeline_type)
    if not parser:
        log.warning(f"No parser for pipeline type: {pipeline_type}")
        return {}
    return parser(Path(results_dir))
