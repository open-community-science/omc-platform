"""Review endpoints - trigger AI reviews on manuscripts."""
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from .config import get_settings
from .database import get_db, Submission, User
from .auth import require_user

router = APIRouter(prefix="/reviews", tags=["reviews"])
settings = get_settings()


@router.post("/{submission_id}/run")
async def run_reviews(
    submission_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Run all AI review agents on a submission's manuscript.

    Requires the submission to have generated manuscript sections
    (stored in interview_data under _manuscript key).
    """
    from ai.review_agents import run_all_reviews
    from ai.pipeline_parser import parse_pipeline_outputs
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

    return {"submission_id": submission_id, "reviews": reviews}


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
