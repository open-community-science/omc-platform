"""Generate figures from pipeline outputs as Plotly JSON and SVG.

Creates publication-quality figures from parsed pipeline data:
- MAG quality scatter (completeness vs contamination)
- Taxonomy bar chart (phylum distribution)
- Assembly stats summary
- Metabolic pathway heatmap
"""
import json
import logging

log = logging.getLogger(__name__)


def generate_figures(pipeline_outputs: dict, pipeline_type: str = "") -> dict:
    """Generate all applicable figures from pipeline outputs.

    Returns dict of {figure_name: plotly_json_dict}.
    """
    figures = {}

    mag_summary = pipeline_outputs.get("MAG_summary", {})
    if mag_summary:
        fig = mag_quality_scatter(mag_summary)
        if fig:
            figures["mag_quality"] = fig

    taxonomy = pipeline_outputs.get("taxonomy_summary", {})
    if taxonomy:
        fig = taxonomy_bar(taxonomy)
        if fig:
            figures["taxonomy_distribution"] = fig

    assembly = pipeline_outputs.get("assembly_stats", {})
    if assembly:
        fig = assembly_summary(assembly)
        if fig:
            figures["assembly_stats"] = fig

    metabolism = pipeline_outputs.get("metabolism", {})
    if metabolism:
        fig = pathway_summary(metabolism)
        if fig:
            figures["metabolic_pathways"] = fig

    # RNA-seq specific
    de = pipeline_outputs.get("differential_expression", {})
    if de:
        fig = de_summary(de)
        if fig:
            figures["differential_expression"] = fig

    log.info(f"Generated {len(figures)} figures")
    return figures


def mag_quality_scatter(mag_summary: dict) -> dict:
    """MAG quality overview as a summary bar chart.

    Shows counts of high/medium/low quality bins.
    """
    hq = mag_summary.get("high_quality", 0)
    mq = mag_summary.get("medium_quality", 0)
    lq = mag_summary.get("low_quality", 0)

    if not any([hq, mq, lq]):
        return {}

    return {
        "data": [{
            "type": "bar",
            "x": ["High Quality\n(≥90% comp, <5% cont)",
                  "Medium Quality\n(≥50% comp, <10% cont)",
                  "Low Quality\n(<50% comp)"],
            "y": [hq, mq, lq],
            "marker": {"color": ["#2ecc71", "#f39c12", "#e74c3c"]},
            "text": [str(hq), str(mq), str(lq)],
            "textposition": "auto",
        }],
        "layout": {
            "title": "MAG Quality Distribution",
            "yaxis": {"title": "Number of MAGs"},
            "template": "plotly_white",
            "height": 400,
            "margin": {"b": 100},
        },
    }


def taxonomy_bar(taxonomy: dict) -> dict:
    """Phylum-level taxonomy distribution bar chart."""
    phyla = taxonomy.get("phyla", {})
    if not phyla:
        return {}

    # Top 10 phyla
    sorted_phyla = sorted(phyla.items(), key=lambda x: -x[1])[:10]
    names = [p[0] for p in sorted_phyla]
    counts = [p[1] for p in sorted_phyla]

    return {
        "data": [{
            "type": "bar",
            "x": names,
            "y": counts,
            "marker": {"color": "#3498db"},
            "text": [str(c) for c in counts],
            "textposition": "auto",
        }],
        "layout": {
            "title": "Taxonomic Distribution (Phylum Level)",
            "xaxis": {"title": "Phylum", "tickangle": -45},
            "yaxis": {"title": "Number of MAGs"},
            "template": "plotly_white",
            "height": 450,
            "margin": {"b": 120},
        },
    }


def assembly_summary(assembly: dict) -> dict:
    """Assembly statistics summary as an indicator panel."""
    metrics = []
    labels = []

    if assembly.get("total_contigs"):
        metrics.append(assembly["total_contigs"])
        labels.append("Total Contigs")
    if assembly.get("total_length_mb"):
        metrics.append(assembly["total_length_mb"])
        labels.append("Total Length (Mb)")
    if assembly.get("n50_kb"):
        metrics.append(assembly["n50_kb"])
        labels.append("N50 (kb)")
    if assembly.get("largest_contig_kb"):
        metrics.append(assembly["largest_contig_kb"])
        labels.append("Largest Contig (kb)")

    if not metrics:
        return {}

    return {
        "data": [{
            "type": "bar",
            "x": labels,
            "y": metrics,
            "marker": {"color": "#9b59b6"},
            "text": [str(m) for m in metrics],
            "textposition": "auto",
        }],
        "layout": {
            "title": "Assembly Statistics",
            "yaxis": {"title": "Value", "type": "log"},
            "template": "plotly_white",
            "height": 400,
        },
    }


def pathway_summary(metabolism: dict) -> dict:
    """Metabolic pathway summary — active vs total pathways."""
    active = metabolism.get("active_pathways", 0)
    total = metabolism.get("total_pathways_tested", 0)

    if not total:
        return {}

    return {
        "data": [{
            "type": "pie",
            "labels": ["Active Pathways", "Inactive/Absent"],
            "values": [active, total - active],
            "marker": {"colors": ["#2ecc71", "#ecf0f1"]},
            "textinfo": "label+value",
            "hole": 0.4,
        }],
        "layout": {
            "title": f"Metabolic Pathways ({active}/{total} active)",
            "template": "plotly_white",
            "height": 400,
        },
    }


def de_summary(de_data: dict) -> dict:
    """Differential expression summary bar chart."""
    up = de_data.get("upregulated", 0)
    down = de_data.get("downregulated", 0)
    total = de_data.get("total_genes_tested", 0)
    ns = total - up - down

    if not total:
        return {}

    return {
        "data": [{
            "type": "bar",
            "x": ["Upregulated", "Downregulated", "Not Significant"],
            "y": [up, down, ns],
            "marker": {"color": ["#e74c3c", "#3498db", "#95a5a6"]},
            "text": [str(up), str(down), str(ns)],
            "textposition": "auto",
        }],
        "layout": {
            "title": f"Differential Expression (padj < 0.05)",
            "yaxis": {"title": "Number of Genes"},
            "template": "plotly_white",
            "height": 400,
        },
    }


def figures_to_markdown_refs(figures: dict) -> str:
    """Generate markdown figure references for inclusion in manuscript."""
    if not figures:
        return ""

    refs = []
    captions = {
        "mag_quality": "Distribution of metagenome-assembled genome (MAG) quality categories based on CheckM2 completeness and contamination metrics.",
        "taxonomy_distribution": "Phylum-level taxonomic distribution of classified MAGs based on GTDB-Tk classification.",
        "assembly_stats": "Summary statistics of the metagenome assembly.",
        "metabolic_pathways": "Overview of predicted metabolic pathways identified by MinPath analysis.",
        "differential_expression": "Summary of differentially expressed genes (adjusted p-value < 0.05).",
    }

    for i, (name, _) in enumerate(figures.items(), 1):
        caption = captions.get(name, f"Figure {i}")
        refs.append(f"**Figure {i}.** {caption}")

    return "\n\n".join(refs)
