"""Test figure generation from pipeline outputs."""
import sys
import pytest

sys.path.insert(0, "/data/omc/omc-platform")


def test_mag_quality_figure():
    """Generate MAG quality bar chart from CheckM2 data."""
    from ai.figure_generator import mag_quality_scatter

    mag_data = {
        "total_bins": 177,
        "high_quality": 12,
        "medium_quality": 45,
        "low_quality": 120,
    }
    fig = mag_quality_scatter(mag_data)
    assert fig
    assert fig["data"][0]["type"] == "bar"
    assert fig["data"][0]["y"] == [12, 45, 120]
    assert "MAG Quality" in fig["layout"]["title"]


def test_taxonomy_bar_figure():
    """Generate taxonomy distribution from GTDB-Tk data."""
    from ai.figure_generator import taxonomy_bar

    tax_data = {
        "phyla": {
            "Pseudomonadota": 45,
            "Bacteroidota": 30,
            "Cyanobacteriota": 15,
            "Actinomycetota": 10,
        }
    }
    fig = taxonomy_bar(tax_data)
    assert fig
    assert fig["data"][0]["x"][0] == "Pseudomonadota"
    assert fig["data"][0]["y"][0] == 45


def test_assembly_summary_figure():
    """Generate assembly stats summary."""
    from ai.figure_generator import assembly_summary

    asm_data = {
        "total_contigs": 6050,
        "total_length_mb": 41.46,
        "n50_kb": 9.5,
        "largest_contig_kb": 150.2,
    }
    fig = assembly_summary(asm_data)
    assert fig
    assert len(fig["data"][0]["x"]) == 4


def test_generate_all_figures():
    """Generate all figures from a full pipeline output."""
    from ai.figure_generator import generate_figures

    outputs = {
        "MAG_summary": {"high_quality": 5, "medium_quality": 20, "low_quality": 50},
        "taxonomy_summary": {"phyla": {"Proteobacteria": 30, "Firmicutes": 20}},
        "assembly_stats": {"total_contigs": 1000, "n50_kb": 15.0},
        "metabolism": {"active_pathways": 171, "total_pathways_tested": 400},
    }
    figures = generate_figures(outputs, "nanopore_mag")
    assert len(figures) == 4
    assert "mag_quality" in figures
    assert "taxonomy_distribution" in figures
    assert "assembly_stats" in figures
    assert "metabolic_pathways" in figures
    print(f"\nGenerated {len(figures)} figures: {list(figures.keys())}")


def test_empty_outputs_no_figures():
    """Empty pipeline outputs produce no figures."""
    from ai.figure_generator import generate_figures

    figures = generate_figures({}, "nanopore_mag")
    assert figures == {}
