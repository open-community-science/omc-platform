"""Parse Nextflow pipeline outputs into structured data for manuscript generation.

Supports multiple pipeline types:
- nanopore_mag: Nanopore Metagenome = nanopore_assembly -> mag_analysis
  (mag_analysis output under <results>/mag; assembly under <results>/assembly)
- illumina_mag: Illumina Metagenome = illumina_assembly -> mag_analysis
  (per-sample; mag_analysis output under <results>/mag/<sample>)
- illumina_amplicon: Illumina Amplicons (DADA2 ASVs + taxonomy + diversity)
- rnaseq: Transcript quantification + differential expression (on hold)
- isolate_genome: Single-genome assembly + annotation (submitted as metagenome)
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
        if not lines:
            return []
        # Find header among comment lines: last # line with alpha chars (skip separator lines like "# --- ---")
        header_idx = 0
        for i, line in enumerate(lines):
            if not line.startswith("#"):
                break
            stripped = line.lstrip("#").strip()
            if any(c.isalpha() for c in stripped):
                header_idx = i
        # Strip only the # prefix from header line (preserve leading whitespace for alignment)
        header = lines[header_idx].lstrip("#")
        # Data lines are everything after the header block that doesn't start with #
        data = [l for l in lines[header_idx + 1:] if not l.startswith("#")]
        return list(csv.DictReader([header] + data, delimiter="\t", **kwargs))
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
    info_path = results_dir / "assembly/assembly_info.txt"
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
    """Parse metabolic analysis results from multiple sources."""
    result = {}

    # MinPath pathway predictions
    rows = _read_tsv(results_dir / "metabolism/minpath/minpath_pathways.tsv")
    if rows:
        community = [r for r in rows if r.get("mag_id") == "_community"]
        active = [r for r in community if r.get("minpath") == "1"]
        result["minpath"] = {
            "total_pathways_tested": len(community),
            "active_pathways": len(active),
            "top_pathways": [{"id": r["pathway_id"], "name": r["pathway_name"],
                              "families_found": int(r["found_families"]),
                              "families_total": int(r["total_families"])}
                             for r in active[:10]],
        }

    # KEGG module completeness
    rows = _read_tsv(results_dir / "metabolism/modules/module_completeness.tsv")
    if rows:
        community_row = next((r for r in rows if r.get("mag_id") == "_community"), None)
        if community_row:
            # Columns are module IDs like "M00001:Glycolysis..."
            active_modules = []
            for col, val in community_row.items():
                if col == "mag_id":
                    continue
                try:
                    completeness = float(val)
                except (ValueError, TypeError):
                    continue
                if completeness > 0:
                    active_modules.append({"module": col, "completeness": completeness})
            active_modules.sort(key=lambda x: -x["completeness"])
            result["module_completeness"] = {
                "total_modules": len([c for c in community_row if c != "mag_id"]),
                "active_modules": len(active_modules),
                "top_modules": active_modules[:15],
            }

    # KOfam scan results
    rows = _read_tsv(results_dir / "metabolism/kofamscan/kofamscan_results.tsv")
    if rows:
        kos = {}
        for r in rows:
            ko = r.get("KO", "")
            if ko:
                kos[ko] = kos.get(ko, 0) + 1
        result["kofamscan"] = {
            "total_annotations": len(rows),
            "unique_kos": len(kos),
        }

    return result


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


def parse_rrna(results_dir: Path) -> dict:
    """Parse rRNA/tRNA gene detection results."""
    genes = _read_tsv(results_dir / "taxonomy/rrna/rrna_genes.tsv")
    trna = _read_tsv(results_dir / "taxonomy/rrna/trna_genes.tsv")
    if not genes and not trna:
        return {}
    result = {}
    if genes:
        types = {}
        for r in genes:
            t = r.get("rrna_type", "unknown")
            types[t] = types.get(t, 0) + 1
        result["rrna_genes"] = len(genes)
        result["rrna_types"] = types
    if trna:
        result["trna_genes"] = len(trna)
    return result


def parse_contig_taxonomy(results_dir: Path) -> dict:
    """Parse contig-level taxonomy from kaiju and kraken2."""
    result = {}
    # Kaiju
    kaiju = _read_tsv(results_dir / "taxonomy/kaiju/kaiju_contigs.tsv")
    if kaiju:
        classified = [r for r in kaiju if r.get("status") == "C"]
        lineages = {}
        for r in classified:
            # Extract phylum from lineage
            lin = r.get("lineage", "")
            parts = [p.strip() for p in lin.split(";")]
            phylum = parts[1] if len(parts) > 1 else "Unknown"
            lineages[phylum] = lineages.get(phylum, 0) + 1
        result["kaiju"] = {
            "total_contigs": len(kaiju),
            "classified": len(classified),
            "phyla": dict(sorted(lineages.items(), key=lambda x: -x[1])),
        }
    # Kraken2
    kraken = _read_tsv(results_dir / "taxonomy/kraken2/kraken2_contigs.tsv")
    if kraken:
        classified = [r for r in kraken if r.get("status") == "C"]
        result["kraken2"] = {
            "total_contigs": len(kraken),
            "classified": len(classified),
        }
    return result


def parse_annotation(results_dir: Path) -> dict:
    """Parse gene annotation summary."""
    # Try bakta basic first, then extra
    for subdir in ["bakta/basic", "bakta/extra"]:
        path = results_dir / f"annotation/{subdir}/annotation.tsv"
        if path.exists():
            rows = _read_tsv(path)
            if rows:
                # Count feature types from the Type column
                types = {}
                for r in rows:
                    t = r.get("Type", r.get("type", ""))
                    if t:
                        types[t] = types.get(t, 0) + 1
                return {
                    "method": "Bakta",
                    "total_features": len(rows),
                    "feature_types": types,
                }
    return {}


def parse_eukaryotic(results_dir: Path) -> dict:
    """Parse eukaryotic detection results (tiara, whokaryote)."""
    result = {}
    euk_dir = results_dir / "eukaryotic"
    if not euk_dir.exists():
        return {}

    # Tiara classification
    tiara = _read_tsv(euk_dir / "tiara/tiara_output.tsv")
    if tiara:
        classes = {}
        for r in tiara:
            c = r.get("class_fst_stage", "unknown")
            classes[c] = classes.get(c, 0) + 1
        result["tiara"] = {
            "total_contigs": len(tiara),
            "classifications": classes,
        }

    # MarFERReT
    marferret = _read_tsv(euk_dir / "marferret/marferret_contigs.tsv")
    if marferret:
        classified = [r for r in marferret if r.get("n_classified", "0") != "0"]
        result["marferret"] = {
            "total_contigs": len(marferret),
            "with_eukaryotic_hits": len(classified),
        }

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


def _mag_sample_roots(results_dir: Path) -> list[Path]:
    """Locate the mag_analysis output root(s) within a run's results.

    OMC's metagenome pipelines are `<assembler> -> mag_analysis`, so
    mag_analysis publishes under <results_dir>/mag. Nanopore is a single
    co-assembly (<results_dir>/mag/binning/...); illumina is per-sample
    (<results_dir>/mag/<sample>/binning/...). Falls back to the legacy flat
    layout (<results_dir>/binning/...). Returns [] if no binning output.
    """
    results_dir = Path(results_dir)
    mag = results_dir / "mag"
    if (mag / "binning").is_dir():
        return [mag]                                    # single (nanopore)
    if mag.is_dir():
        subs = [d for d in sorted(mag.iterdir()) if (d / "binning").is_dir()]
        if subs:
            return subs                                 # per-sample (illumina)
    if (results_dir / "binning").is_dir():
        return [results_dir]                            # legacy flat layout
    return []


def _parse_mag_root(root: Path) -> dict:
    """Parse the mag_analysis output categories under a single root dir
    (binning / taxonomy / annotation / metabolism / eukaryotic / MGE)."""
    outputs = {}
    checkm2 = parse_checkm2(root)
    if checkm2:
        outputs["MAG_summary"] = checkm2
    dastool = parse_dastool(root)
    if dastool:
        outputs["consensus_binning"] = dastool
    gtdbtk = parse_gtdbtk(root)
    if gtdbtk:
        outputs["taxonomy_summary"] = gtdbtk
    contig_tax = parse_contig_taxonomy(root)
    if contig_tax:
        outputs["contig_taxonomy"] = contig_tax
    rrna = parse_rrna(root)
    if rrna:
        outputs["rrna_trna"] = rrna
    annotation = parse_annotation(root)
    if annotation:
        outputs["annotation"] = annotation
    metabolism = parse_metabolism(root)
    if metabolism:
        outputs["metabolism"] = metabolism
    mge = parse_mge(root)
    if mge:
        outputs["mobile_genetic_elements"] = mge
    eukaryotic = parse_eukaryotic(root)
    if eukaryotic:
        outputs["eukaryotic"] = eukaryotic
    binning = root / "binning"
    if binning.is_dir():
        binner_dirs = [d.name for d in binning.iterdir()
                       if d.is_dir() and d.name not in ("checkm2", "dastool")]
        if binner_dirs:
            outputs["binners_used"] = binner_dirs
    return outputs


def parse_nanopore_mag(results_dir: Path) -> dict:
    """Parse all outputs from a Nanopore Metagenome run.

    Composes nanopore_assembly (<results_dir>/assembly) + mag_analysis
    (<results_dir>/mag). Returns a structured dict for manuscript generation.
    """
    results_dir = Path(results_dir)
    if not results_dir.exists():
        log.warning(f"Results directory not found: {results_dir}")
        return {}

    outputs = {}

    # mag_analysis outputs (single co-assembly root, or legacy flat)
    roots = _mag_sample_roots(results_dir)
    if roots:
        outputs.update(_parse_mag_root(roots[0]))

    # Assembly stats + timing live at the run root
    assembly = parse_assembly(results_dir)
    if assembly:
        outputs["assembly_stats"] = assembly
    timing = parse_pipeline_timing(results_dir)
    if timing:
        outputs["pipeline_info"] = timing

    log.info(f"Parsed {len(outputs)} output categories from {results_dir}")
    return outputs


def parse_illumina_assembly(results_dir: Path) -> dict:
    """Parse MEGAHIT or metaSPAdes assembly stats."""
    # Try MEGAHIT first, then metaSPAdes
    for asm_dir in ["megahit", "metaspades"]:
        stats_path = results_dir / f"assembly/{asm_dir}/final.contigs.stats.tsv"
        if stats_path.exists():
            rows = _read_tsv(stats_path)
            if rows:
                lengths = [int(r.get("length", 0)) for r in rows]
                return {
                    "assembler": asm_dir,
                    "total_contigs": len(rows),
                    "total_length_mb": round(sum(lengths) / 1e6, 2),
                    "n50_kb": round(_calc_n50(lengths) / 1e3, 1),
                    "largest_contig_kb": round(max(lengths) / 1e3, 1) if lengths else 0,
                }

    # Try QUAST report as fallback
    quast_path = results_dir / "assembly/quast/report.tsv"
    rows = _read_tsv(quast_path)
    if rows:
        r = rows[0]
        return {
            "assembler": r.get("Assembly", "unknown"),
            "total_contigs": int(r.get("# contigs", 0)),
            "total_length_mb": round(int(r.get("Total length", 0)) / 1e6, 2),
            "n50_kb": round(int(r.get("N50", 0)) / 1e3, 1),
            "largest_contig_kb": round(int(r.get("Largest contig", 0)) / 1e3, 1),
        }
    return {}


def parse_illumina_mag(results_dir: Path) -> dict:
    """Parse all outputs from an Illumina Metagenome run.

    Composes illumina_assembly (<results_dir>/assembly) + mag_analysis. Illumina
    assembly + MAG recovery are per-sample, so mag_analysis output lives under
    <results_dir>/mag/<sample>/. Shares the mag_analysis components (CheckM2,
    GTDB-Tk, DAS Tool, ...) with the nanopore pipeline.
    """
    results_dir = Path(results_dir)
    if not results_dir.exists():
        log.warning(f"Results directory not found: {results_dir}")
        return {}

    outputs = {}

    assembly = parse_illumina_assembly(results_dir)
    if assembly:
        outputs["assembly_stats"] = assembly

    roots = _mag_sample_roots(results_dir)
    if len(roots) == 1:
        outputs.update(_parse_mag_root(roots[0]))
    elif len(roots) > 1:
        # Per-sample MAG recovery: summarise each sample and the total bin count.
        per_sample = {}
        total_bins = 0
        for root in roots:
            parsed = _parse_mag_root(root)
            if parsed:
                per_sample[root.name] = parsed
                total_bins += parsed.get("MAG_summary", {}).get("total_bins", 0)
        if per_sample:
            outputs["samples"] = per_sample
            outputs["n_samples"] = len(per_sample)
            outputs["total_bins_all_samples"] = total_bins

    timing = parse_pipeline_timing(results_dir)
    if timing:
        outputs["pipeline_info"] = timing

    log.info(f"Parsed {len(outputs)} output categories from {results_dir}")
    return outputs


def parse_rnaseq(results_dir: Path) -> dict:
    """Parse RNA-seq pipeline outputs.

    Expects: salmon/quant results, DESeq2 results, FastQC/MultiQC.
    """
    results_dir = Path(results_dir)
    if not results_dir.exists():
        return {}

    outputs = {}

    # Salmon quantification summary
    quant_path = results_dir / "quantification/salmon/quant_summary.tsv"
    rows = _read_tsv(quant_path)
    if rows:
        mapping_rates = [float(r.get("mapping_rate", 0)) for r in rows]
        outputs["quantification"] = {
            "method": "Salmon",
            "samples": len(rows),
            "mean_mapping_rate": round(sum(mapping_rates) / len(mapping_rates), 1) if mapping_rates else 0,
        }

    # DESeq2 differential expression
    de_path = results_dir / "differential_expression/deseq2/results.tsv"
    de_rows = _read_tsv(de_path)
    if de_rows:
        sig = [r for r in de_rows if float(r.get("padj", 1)) < 0.05]
        up = [r for r in sig if float(r.get("log2FoldChange", 0)) > 0]
        down = [r for r in sig if float(r.get("log2FoldChange", 0)) < 0]
        outputs["differential_expression"] = {
            "method": "DESeq2",
            "total_genes_tested": len(de_rows),
            "significant_genes": len(sig),
            "upregulated": len(up),
            "downregulated": len(down),
        }

    timing = parse_pipeline_timing(results_dir)
    if timing:
        outputs["pipeline_info"] = timing

    log.info(f"Parsed {len(outputs)} output categories from {results_dir}")
    return outputs


def parse_isolate_genome(results_dir: Path) -> dict:
    """Parse isolate genome assembly + annotation outputs.

    Expects: assembly stats, Bakta annotation, CheckM2 quality.
    """
    results_dir = Path(results_dir)
    if not results_dir.exists():
        return {}

    outputs = {}

    # Assembly (could be Flye or MEGAHIT depending on sequencing)
    assembly = parse_assembly(results_dir)
    if not assembly:
        assembly = parse_illumina_assembly(results_dir)
    if assembly:
        outputs["assembly_stats"] = assembly

    # CheckM2
    checkm2 = parse_checkm2(results_dir)
    if checkm2:
        outputs["genome_quality"] = checkm2

    # GTDB-Tk
    gtdbtk = parse_gtdbtk(results_dir)
    if gtdbtk:
        outputs["taxonomy"] = gtdbtk

    # Bakta annotation summary
    bakta_path = results_dir / "annotation/bakta/summary.tsv"
    rows = _read_tsv(bakta_path)
    if rows:
        outputs["annotation"] = {
            "method": "Bakta",
            "features": {r.get("feature_type", ""): int(r.get("count", 0)) for r in rows},
        }

    timing = parse_pipeline_timing(results_dir)
    if timing:
        outputs["pipeline_info"] = timing

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


def parse_amplicon_asv_table(results_dir: Path) -> dict:
    """ASV and sample counts from the final merged table (seqtab_final/)."""
    wide = _read_tsv(results_dir / "seqtab_final/seqtab_final_wide.tsv")
    if not wide:
        return {}
    cols = list(wide[0].keys())
    # Everything that isn't an ASV/sequence identifier column is a sample.
    id_like = {"asv", "asv_id", "id", "sequence", "seq", "feature", "feature_id",
               "otu", "otu_id", "taxon", ""}
    sample_cols = [c for c in cols if c.strip().lower() not in id_like]
    out = {"total_asvs": len(wide), "n_samples": len(sample_cols)}
    if sample_cols:
        out["samples"] = sample_cols[:50]
    return out


def parse_amplicon_taxonomy(results_dir: Path) -> dict:
    """Summarise the primary taxonomy assignment (taxonomy/<db>_taxonomy.tsv)."""
    tax_dir = results_dir / "taxonomy"
    if not tax_dir.exists():
        return {}
    tax_files = sorted(tax_dir.glob("*_taxonomy.tsv"))
    if not tax_files:
        return {}
    rows = _read_tsv(tax_files[0])
    if not rows:
        return {}

    ranks = ["Domain", "Kingdom", "Phylum", "Class", "Order", "Family", "Genus", "Species"]
    colmap = {c.strip().lower(): c for c in rows[0].keys()}
    unassigned = {"", "na", "nana", "unclassified", "unassigned", "none"}

    def _val(row, rank):
        c = colmap.get(rank.lower())
        if not c:
            return None
        v = (row.get(c) or "").strip()
        return None if v.lower() in unassigned else v

    per_rank = {}
    for rank in ranks:
        if rank.lower() in colmap:
            per_rank[rank.lower()] = sum(1 for r in rows if _val(r, rank) is not None)

    top_phyla = {}
    if "phylum" in colmap:
        from collections import Counter
        counts = Counter(v for r in rows if (v := _val(r, "Phylum")) is not None)
        top_phyla = dict(counts.most_common(5))

    return {
        "database": tax_files[0].name.replace("_taxonomy.tsv", ""),
        "databases_used": [f.name.replace("_taxonomy.tsv", "") for f in tax_files],
        "total_asvs_classified": len(rows),
        "classified_per_rank": per_rank,
        "top_phyla": top_phyla,
        "phyla": top_phyla,  # alias so figure_generator's taxonomy chart renders
    }


def parse_amplicon_filtering(results_dir: Path) -> dict:
    """Read-retention summary from per-sample filtered/*_filt_stats.tsv."""
    filt_dir = results_dir / "filtered"
    if not filt_dir.exists():
        return {}
    stats_files = sorted(filt_dir.glob("*_filt_stats.tsv"))
    if not stats_files:
        return {}
    reads_in = reads_out = 0
    n = 0
    for sf in stats_files:
        for r in _read_tsv(sf):
            lower = {k.strip().lower(): v for k, v in r.items()}
            try:
                # column names vary across dada2 versions; match leniently
                ri = next((float(lower[k]) for k in lower if "input" in k or k in ("reads.in", "reads_in")), None)
                ro = next((float(lower[k]) for k in lower if "nonchim" in k or "output" in k or k in ("reads.out", "reads_out")), None)
                if ri is not None:
                    reads_in += ri; n += 1
                if ro is not None:
                    reads_out += ro
            except (ValueError, TypeError):
                continue
    out = {"n_samples": len(stats_files)}
    if reads_in and reads_out:
        out["reads_input"] = int(reads_in)
        out["reads_retained"] = int(reads_out)
        out["retention_pct"] = round(100 * reads_out / reads_in, 1)
    return out


def parse_amplicon(results_dir: Path) -> dict:
    """Parse outputs from an Illumina amplicon pipeline run.

    Reads the final ASV table, taxonomy assignment, and denoising stats.
    Every sub-parser degrades to {} if its files are absent, so partial runs
    still yield whatever is available. NOTE: field/column assumptions are
    drawn from the illumina_amplicon module publishDir specs and should be
    validated against a real completed run.
    """
    results_dir = Path(results_dir)
    if not results_dir.exists():
        log.warning(f"Results directory not found: {results_dir}")
        return {}

    outputs = {}
    for key, fn in (("asv_summary", parse_amplicon_asv_table),
                    ("taxonomy_summary", parse_amplicon_taxonomy),
                    ("filtering", parse_amplicon_filtering)):
        data = fn(results_dir)
        if data:
            outputs[key] = data

    known = {"taxonomy", "seqtab_final", "seqtabs", "filtered", "clustering",
             "network", "phylogeny", "renormalized", "viz", "quality_check"}
    present = [d.name for d in results_dir.iterdir() if d.is_dir() and d.name in known]
    if present:
        outputs["result_categories"] = sorted(present)

    log.info(f"Parsed {len(outputs)} output categories from {results_dir}")
    return outputs


# Pipeline parser registry
PARSERS = {
    "nanopore_mag": parse_nanopore_mag,
    "illumina_mag": parse_illumina_mag,
    "illumina_amplicon": parse_amplicon,
    "rnaseq": parse_rnaseq,
    "isolate_genome": parse_isolate_genome,
}


def _read_run_manifest(results_dir: Path) -> dict | None:
    """Read run_manifest.json if the pipeline emitted one — tool versions (container
    images), resolved parameters, and reference DBs. illumina_amplicon writes it to
    ``viz/``, danaSeq to the results root (see microscape-nf #5 / danaSeq #24)."""
    import json
    for cand in (results_dir / "run_manifest.json", results_dir / "viz" / "run_manifest.json"):
        try:
            if cand.exists():
                return json.loads(cand.read_text())
        except Exception as e:
            log.warning(f"Could not read run manifest {cand}: {e}")
    return None


def parse_pipeline_outputs(pipeline_type: str, results_dir: str | Path) -> dict:
    """Parse outputs for any supported pipeline type."""
    parser = PARSERS.get(pipeline_type)
    if not parser:
        log.warning(f"No parser for pipeline type: {pipeline_type}")
        return {}
    results_dir = Path(results_dir)
    outputs = parser(results_dir)
    manifest = _read_run_manifest(results_dir)
    if manifest:
        outputs["run_manifest"] = manifest
    return outputs
