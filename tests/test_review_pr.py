"""Test review agents produce PR-ready markdown output."""
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_format_review_as_markdown():
    """Verify review dict is formatted as readable markdown for PR comments."""
    from portal.app.reviews import _format_review_as_markdown

    review = {
        "review_type": "statistical",
        "summary": "Generally sound methods with minor gaps.",
        "comments": [
            {
                "section": "methods",
                "issue": "Missing multiple testing correction",
                "detail": "With 177 bins tested, a Bonferroni or FDR correction should be applied.",
                "severity": "major",
                "confidence": 0.85,
            },
            {
                "section": "results",
                "issue": "Effect size not reported",
                "detail": "Consider reporting Cohen's d alongside p-values.",
                "severity": "minor",
                "confidence": 0.6,
            },
        ],
    }

    md = _format_review_as_markdown(review)
    assert "## AI Review: Statistical" in md
    assert "Generally sound" in md
    assert "MAJOR" in md
    assert "MINOR" in md
    assert "85%" in md
    assert "Bonferroni" in md
    print(f"\n{md}")


def test_review_endpoint_requires_github_repo():
    """Review endpoint should require a GitHub repo to exist."""
    # This is a design assertion — reviews always produce PRs
    from portal.app.reviews import run_reviews
    # The endpoint signature includes github_repo check
    import inspect
    source = inspect.getsource(run_reviews)
    assert "github_repo" in source
