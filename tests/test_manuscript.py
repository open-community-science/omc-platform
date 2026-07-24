"""Test manuscript generation with LM Studio — full pipeline."""
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ai.manuscript_generator import generate_manuscript_draft

pytestmark = pytest.mark.ai

INTERVIEW_DATA = {
    "research_question": "How do temperature gradients shape marine microbial community diversity across ocean basins?",
    "study_context": "Samples from tropical to polar waters, 0-4000m depth, coastal and open ocean sites.",
    "sample_info": "486 samples, size-fractionated, 16S amplicon on Illumina MiSeq.",
    "expected_findings": "Temperature and depth as primary drivers. SAR11 and Prochlorococcus in surface waters.",
    "broader_significance": "Predicting ocean microbiome response to climate change.",
    "limitations": "Amplicon-only, single time point, potential primer bias against archaea.",
    "additional_context": "Environmental metadata (T, S, O2, nutrients) collected concurrently.",
}

# Simulated pipeline outputs (what a real Nextflow run would produce)
PIPELINE_OUTPUTS = {
    "taxonomy_summary": {
        "total_ASVs": 12847,
        "total_reads_passing_QC": 48_293_102,
        "phyla_detected": 42,
        "dominant_phyla": [
            {"name": "Proteobacteria", "relative_abundance": 0.34},
            {"name": "Bacteroidota", "relative_abundance": 0.18},
            {"name": "Cyanobacteria", "relative_abundance": 0.12},
            {"name": "Actinobacteriota", "relative_abundance": 0.09},
            {"name": "Verrucomicrobiota", "relative_abundance": 0.06},
        ],
    },
    "alpha_diversity": {
        "shannon_mean": 4.82,
        "shannon_sd": 1.23,
        "observed_features_mean": 312,
        "samples_analyzed": 486,
        "correlation_with_temperature": {"r": -0.42, "p": 0.001},
        "correlation_with_depth": {"r": 0.38, "p": 0.003},
    },
    "beta_diversity": {
        "method": "weighted_unifrac",
        "permanova_temperature": {"pseudo_F": 12.4, "p": 0.001, "R2": 0.18},
        "permanova_depth": {"pseudo_F": 9.7, "p": 0.001, "R2": 0.14},
        "permanova_region": {"pseudo_F": 5.2, "p": 0.003, "R2": 0.08},
    },
    "differential_abundance": {
        "method": "ANCOM-BC",
        "significant_ASVs": 234,
        "enriched_in_surface": ["Prochlorococcus", "SAR11 clade", "Synechococcus"],
        "enriched_in_deep": ["Thaumarchaeota", "SAR324", "Marinimicrobia"],
    },
    "MAG_summary": {
        "total_bins": 87,
        "high_quality": 23,
        "medium_quality": 41,
        "completeness_mean": 72.3,
        "contamination_mean": 3.1,
        "novel_species": 12,
    },
}


@pytest.mark.asyncio
@pytest.mark.timeout(600)
async def test_generate_full_manuscript():
    """Generate all 5 sections from simulated pipeline data + interview."""
    sections = await generate_manuscript_draft(
        PIPELINE_OUTPUTS,
        INTERVIEW_DATA,
        "illumina_mag",
        "PRJNA656268",
    )

    expected = ["introduction", "methods", "results", "discussion", "abstract"]
    for sec in expected:
        assert sec in sections, f"Missing section: {sec}"
        assert isinstance(sections[sec], str)
        assert len(sections[sec]) > 100, f"Section '{sec}' too short: {len(sections[sec])} chars"
        print(f"\n{'='*60}")
        print(f"  {sec.upper()} ({len(sections[sec])} chars)")
        print(f"{'='*60}")
        print(sections[sec][:300] + "...")
