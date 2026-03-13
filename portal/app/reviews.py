"""Review endpoints - trigger AI reviews on manuscripts.

Reviews always produce GitHub PRs on the paper's repository.
Each review type (statistical, methodological, clarity) gets its own PR
with structured comments, matching OMC's git-native review model.
"""
import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import attributes
from sqlalchemy import select

from .config import get_settings
from .database import get_db, Submission, User
from .auth import require_user

router = APIRouter(prefix="/reviews", tags=["reviews"])
settings = get_settings()
logger = logging.getLogger(__name__)


def _format_review_as_markdown(review: dict) -> str:
    """Convert a structured review dict into a readable markdown comment."""
    review_type = review.get("review_type", "general")
    lines = [f"## AI Review: {review_type.title()}\n"]

    if review.get("summary"):
        lines.append(f"**Summary:** {review['summary']}\n")

    for comment in review.get("comments", []):
        severity = comment.get("severity", "suggestion")
        confidence = comment.get("confidence", 0.5)
        badge = {"critical": "🔴", "major": "🟠", "minor": "🟡", "suggestion": "💡"}.get(severity, "💡")

        lines.append(f"### {badge} [{severity.upper()}] {comment.get('issue', '')}")
        lines.append(f"**Section:** {comment.get('section', 'general')} · **Confidence:** {confidence:.0%}\n")
        lines.append(comment.get("detail", ""))
        lines.append("")

    return "\n".join(lines)


@router.post("/{submission_id}/run")
async def run_reviews(
    submission_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Run all AI review agents on a submission's manuscript.

    Each review agent produces a GitHub PR on the paper repo.
    Requires the submission to have a generated manuscript and a GitHub repo.
    """
    from ai.review_agents import run_all_reviews
    from ai.pipeline_parser import parse_pipeline_outputs
    from .github_integration import create_review_pr
    from pathlib import Path

    stmt = select(Submission).where(
        Submission.id == submission_id,
        Submission.user_id == user.id,
    )
    result = await db.execute(stmt)
    submission = result.scalar_one_or_none()
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")

    interview_data = submission.interview_data or {}
    manuscript = interview_data.get("_manuscript")
    if not manuscript:
        raise HTTPException(
            status_code=400,
            detail="No manuscript found. Generate manuscript first.",
        )

    has_github_repo = bool(submission.github_repo)

    # Parse pipeline outputs for statistical review
    pipeline_outputs = {}
    try:
        results_path = Path(settings.results_path) / submission.bioproject_accession
        pipeline_outputs = parse_pipeline_outputs(
            submission.pipeline.value, results_path
        )
    except Exception:
        pass

    pipeline_config = {
        "pipeline": submission.pipeline.value,
        "accession": submission.bioproject_accession,
    }

    reviews = await run_all_reviews(
        manuscript,
        pipeline_outputs,
        pipeline_config,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
    )

    # Create PRs if the paper has a GitHub repo
    pr_urls = []
    if has_github_repo:
        # e.g. "https://github.com/omc-papers/paper-0001" → "omc-papers/paper-0001"
        repo_full_name = "/".join(submission.github_repo.rstrip("/").split("/")[-2:])

        for review in reviews:
            review_type = review.get("review_type", "general")
            body_md = _format_review_as_markdown(review)

            try:
                pr_url = await create_review_pr(
                    repo_full_name,
                    [{"body": body_md}],
                    review_type,
                )
                pr_urls.append({"review_type": review_type, "pr_url": pr_url})
                logger.info(f"Created review PR: {pr_url}")
            except Exception as e:
                logger.error(f"Failed to create {review_type} review PR: {e}")
                pr_urls.append({"review_type": review_type, "error": str(e)})
    else:
        logger.info(f"No GitHub repo for submission {submission_id} — reviews saved without PRs")

    # Store review results and PR URLs in interview_data
    interview_data["_reviews"] = reviews
    interview_data["_review_prs"] = pr_urls
    submission.interview_data = interview_data
    attributes.flag_modified(submission, "interview_data")
    await db.commit()

    return {
        "submission_id": submission_id,
        "reviews": reviews,
        "pull_requests": pr_urls,
    }


@router.post("/{submission_id}/generate")
async def generate_manuscript(
    submission_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Generate manuscript from pipeline outputs and interview data.

    Stores the generated sections in interview_data._manuscript.
    """
    from ai.manuscript_generator import generate_manuscript_draft
    from ai.pipeline_parser import parse_pipeline_outputs
    from sqlalchemy.orm import attributes
    from pathlib import Path

    stmt = select(Submission).where(
        Submission.id == submission_id,
        Submission.user_id == user.id,
    )
    result = await db.execute(stmt)
    submission = result.scalar_one_or_none()
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")

    # Parse pipeline outputs
    pipeline_outputs = {}
    try:
        results_path = Path(settings.results_path) / submission.bioproject_accession
        pipeline_outputs = parse_pipeline_outputs(
            submission.pipeline.value, results_path
        )
    except Exception:
        pass

    # Generate figures from pipeline data
    figures_json = {}
    try:
        from ai.figure_generator import generate_figures
        figures_json = generate_figures(pipeline_outputs, submission.pipeline.value)
    except Exception:
        pass

    interview_data = dict(submission.interview_data or {})

    sections = await generate_manuscript_draft(
        pipeline_outputs=pipeline_outputs,
        interview_data=interview_data,
        pipeline_type=submission.pipeline.value,
        bioproject_accession=submission.bioproject_accession,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
    )

    # Store manuscript in interview_data
    interview_data["_manuscript"] = sections
    submission.interview_data = interview_data
    attributes.flag_modified(submission, "interview_data")

    # Create GitHub paper repo and push manuscript files
    repo_url = None
    if settings.github_token:
        try:
            from .github_integration import create_paper_repo_from_files
            files = _manuscript_to_files(sections, submission, figures_json)
            repo_url = await create_paper_repo_from_files(submission, files)
            submission.github_repo = repo_url
            logger.info(f"Created paper repo: {repo_url}")
        except Exception as e:
            logger.error(f"GitHub repo creation failed: {e}")
            repo_url = None

    await db.commit()

    return {
        "submission_id": submission_id,
        "sections": {k: len(v) for k, v in sections.items()},
        "manuscript": sections,
        "github_repo": repo_url,
    }


def _manuscript_to_files(sections: dict, submission, figures: dict | None = None) -> dict:
    """Convert manuscript sections to Quarto paper repo files.

    Uses the paper-repo template structure for GitHub Actions rendering
    (HTML + PDF via Quarto).
    """
    import json as _json
    from datetime import date

    accession = getattr(submission, "bioproject_accession", "")
    pipeline = getattr(submission, "pipeline", None)
    pipeline_name = pipeline.value if pipeline else "unknown"
    title = submission.title or "Untitled"

    files = {}

    # Quarto manuscript (.qmd)
    abstract = sections.get("abstract", "").replace('"', '\\"')
    qmd = f"""---
title: "{title}"
author:
  - name: ""
    affiliations:
      - ""
date: "{date.today().isoformat()}"
abstract: |
  {abstract}
keywords:
  - microbial ecology
  - metagenomics
bibliography: references.bib
license: "CC BY 4.0"
citation:
  type: article
  container-title: "Open Microbial Community"
---

## Introduction

{sections.get('introduction', '')}

## Methods

{sections.get('methods', '')}

## Results

{sections.get('results', '')}

## Discussion

{sections.get('discussion', '')}

## Data Availability

All sequence data are available from the NCBI SRA under accession [{accession}](https://www.ncbi.nlm.nih.gov/bioproject/{accession}). Analysis code, results, and interactive figures are available in this repository. This paper was generated using the [Open Microbial Community](https://github.com/rec3141/omc-platform) platform.

## References {{.unnumbered}}

::: {{#refs}}
:::
"""
    files["manuscript.qmd"] = qmd

    # Quarto project config
    files["_quarto.yml"] = """project:
  type: manuscript
  output-dir: docs

manuscript:
  article: manuscript.qmd

format:
  html:
    self-contained: true
    toc: true
    toc-depth: 3
    theme:
      light: cosmo
    css: styles.css
    code-fold: true
    fig-responsive: true
  pdf:
    documentclass: article
    geometry:
      - margin=1in
    fontsize: 11pt
    colorlinks: true
"""

    # BibTeX bibliography
    files["references.bib"] = sections.get("bibliography", "")

    # GitHub Actions workflow for auto-rendering
    files[".github/workflows/render.yml"] = """name: Render Paper

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

permissions:
  contents: write
  pages: write

jobs:
  render:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: quarto-dev/quarto-actions/setup@v2

      - name: Render HTML + PDF
        run: quarto render manuscript.qmd

      - name: Deploy to GitHub Pages
        if: github.ref == 'refs/heads/main'
        uses: peaceiris/actions-gh-pages@v3
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          publish_dir: ./docs
"""

    # Minimal CSS
    files["styles.css"] = """body { max-width: 800px; margin: 0 auto; font-family: serif; }
.abstract { font-style: italic; border-left: 3px solid #ccc; padding-left: 1em; }
"""

    # LICENSE
    files["LICENSE"] = "CC-BY-4.0\nhttps://creativecommons.org/licenses/by/4.0/\n"

    # README
    files["README.md"] = f"""# {title}

OMC Paper — AI-assisted scientific manuscript

- **BioProject:** [{accession}](https://www.ncbi.nlm.nih.gov/bioproject/{accession})
- **Pipeline:** {pipeline_name}
- **Generated by:** [OMC Platform](https://github.com/open-community-science/omc-platform)

## Building

```bash
quarto render manuscript.qmd
```

Rendered output appears in `docs/`. GitHub Actions auto-renders on push.

## Review

Reviews are submitted as pull requests on this repository.
"""

    # Also save plain markdown for easy reading
    md_parts = [f"# {title}\n"]
    for name in ["abstract", "introduction", "methods", "results", "discussion"]:
        if name in sections:
            md_parts.append(f"## {name.title()}\n\n{sections[name]}\n")
    files["manuscript/manuscript.md"] = "\n".join(md_parts)

    # Interactive figures as Plotly JSON
    if figures:
        for fig_name, fig_data in figures.items():
            files[f"results/figures/{fig_name}.json"] = _json.dumps(fig_data, indent=2)

    # .omc/ provenance directory — full AI interaction history for training data
    interview_data = getattr(submission, "interview_data", None) or {}

    # Interview transcript (AI conversation with author)
    chat_history = interview_data.get("_chat_history", [])
    if chat_history:
        files[".omc/interview_transcript.json"] = _json.dumps(chat_history, indent=2)

    # First AI draft (before human edits) — this IS the training signal
    files[".omc/manuscript_v1.json"] = _json.dumps(sections, indent=2)

    # SRA/BioProject metadata
    sample_meta = getattr(submission, "sample_metadata", None)
    if sample_meta:
        files[".omc/submission_metadata.json"] = _json.dumps(sample_meta, indent=2, default=str)

    # Pipeline configuration
    files[".omc/pipeline_config.json"] = _json.dumps({
        "pipeline": pipeline_name,
        "bioproject_accession": accession,
        "submission_id": getattr(submission, "id", None),
        "generated_at": date.today().isoformat(),
        "platform": "open-community-science/omc-platform",
    }, indent=2)

    return files
