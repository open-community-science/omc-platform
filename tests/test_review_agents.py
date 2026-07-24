"""Test AI review agents with LM Studio."""
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ai.review_agents import (
    statistical_review, methodological_review, clarity_review,
    completeness_review, biological_plausibility_review, citation_review,
    run_all_reviews,
)

pytestmark = pytest.mark.ai

# Minimal manuscript for review
MANUSCRIPT = {
    "introduction": "Marine microbial communities drive global biogeochemical cycles. We investigated how temperature gradients shape diversity across 486 ocean samples using 16S rRNA amplicon sequencing.",
    "methods": "Samples were collected from global ocean sites. DNA was extracted and 16S rRNA V4 region amplified. Sequences were processed with QIIME2 using DADA2 denoising. Taxonomy assigned against SILVA 138. PERMANOVA tested community differences.",
    "results": "We identified 12,847 ASVs across 42 phyla. Proteobacteria dominated (34%). Temperature significantly correlated with alpha diversity (r=-0.42, p=0.001). PERMANOVA showed temperature explained 18% of community variation (pseudo-F=12.4, p=0.001). 23 high-quality MAGs were recovered.",
    "discussion": "Our findings confirm temperature as a primary driver of marine microbial diversity. The dominance of SAR11 in surface waters is consistent with previous studies. Novel MAGs expand our understanding of deep ocean metabolism.",
    "abstract": "We analyzed 486 ocean metagenomes to assess temperature-driven microbial community structure.",
}

RESULTS_DATA = {
    "alpha_diversity": {"shannon_mean": 4.82, "correlation_with_temperature": {"r": -0.42, "p": 0.001}},
    "beta_diversity": {"permanova_temperature": {"pseudo_F": 12.4, "p": 0.001, "R2": 0.18}},
}

PIPELINE_CONFIG = {
    "pipeline": "illumina_mag",
    "denoiser": "DADA2",
    "taxonomy_db": "SILVA 138",
    "classifier": "sklearn",
}


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_statistical_review():
    result = await statistical_review(MANUSCRIPT, RESULTS_DATA)
    assert "review_type" in result
    assert result["review_type"] == "statistical"
    assert "comments" in result
    assert isinstance(result["comments"], list)
    print(f"\nStatistical review: {len(result['comments'])} comments")
    for c in result["comments"][:3]:
        print(f"  [{c.get('severity', '?')}] {c.get('issue', '?')[:80]}")


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_methodological_review():
    result = await methodological_review(MANUSCRIPT, PIPELINE_CONFIG)
    assert result["review_type"] == "methodological"
    assert len(result["comments"]) > 0
    print(f"\nMethodological review: {len(result['comments'])} comments")
    for c in result["comments"][:3]:
        print(f"  [{c.get('severity', '?')}] {c.get('issue', '?')[:80]}")


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_clarity_review():
    result = await clarity_review(MANUSCRIPT)
    assert result["review_type"] == "clarity"
    assert len(result["comments"]) > 0
    print(f"\nClarity review: {len(result['comments'])} comments")


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_completeness_review():
    result = await completeness_review(MANUSCRIPT, RESULTS_DATA)
    assert result["review_type"] == "completeness"
    assert "comments" in result
    assert isinstance(result["comments"], list)
    print(f"\nCompleteness review: {len(result['comments'])} comments")
    for c in result["comments"][:3]:
        print(f"  [{c.get('severity', '?')}] {c.get('issue', '?')[:80]}")


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_biological_plausibility_review():
    result = await biological_plausibility_review(MANUSCRIPT, RESULTS_DATA)
    assert result["review_type"] == "biological_plausibility"
    assert "comments" in result
    assert isinstance(result["comments"], list)
    print(f"\nBiological plausibility review: {len(result['comments'])} comments")
    for c in result["comments"][:3]:
        print(f"  [{c.get('severity', '?')}] {c.get('issue', '?')[:80]}")


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_citation_review():
    result = await citation_review(MANUSCRIPT)
    assert result["review_type"] == "citation"
    assert "comments" in result
    assert isinstance(result["comments"], list)
    print(f"\nCitation review: {len(result['comments'])} comments")
    for c in result["comments"][:3]:
        print(f"  [{c.get('severity', '?')}] {c.get('issue', '?')[:80]}")
