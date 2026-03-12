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
    await db.commit()

    return {
        "submission_id": submission_id,
        "sections": {k: len(v) for k, v in sections.items()},
        "manuscript": sections,
    }
