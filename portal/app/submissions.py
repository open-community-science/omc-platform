"""Submission routes and logic."""
from fastapi import APIRouter, Request, Depends, HTTPException, Form
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime
from typing import Optional

from .config import get_settings
from .database import get_db, Submission, User, SubmissionStatus, PipelineType
from .auth import require_user
from .slurm import submit_pipeline_job
from .sra_metadata import fetch_sra_metadata

router = APIRouter(prefix="/submissions", tags=["submissions"])
settings = get_settings()


@router.post("/create")
async def create_submission(
    request: Request,
    sra_accession: str = Form(...),
    pipeline: str = Form(...),
    title: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Create a new submission."""
    # Validate SRA accession format (basic check)
    sra_accession = sra_accession.strip().upper()
    if not (sra_accession.startswith(("SRR", "ERR", "DRR", "SRX", "SRP", "PRJNA"))):
        raise HTTPException(status_code=400, detail="Invalid SRA accession format")

    # Validate pipeline
    try:
        pipeline_type = PipelineType(pipeline)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid pipeline selection")

    # Fetch SRA metadata from NCBI
    sra_metadata = await fetch_sra_metadata(sra_accession)

    # Use study title as default paper title if available
    default_title = (
        sra_metadata.get("study_title")
        or sra_metadata.get("title")
        or f"Analysis of {sra_accession}"
    )

    # Create submission
    submission = Submission(
        user_id=user.id,
        sra_accession=sra_accession,
        pipeline=pipeline_type,
        title=title or default_title,
        sample_metadata=sra_metadata,
        status=SubmissionStatus.DRAFT,
    )
    db.add(submission)
    await db.commit()
    await db.refresh(submission)

    return RedirectResponse(url=f"/submissions/{submission.id}", status_code=302)


@router.get("/lookup/{accession}")
async def lookup_accession(accession: str):
    """Live lookup of SRA metadata for the submission form."""
    accession = accession.strip().upper()
    if not accession.startswith(("SRR", "ERR", "DRR", "SRX", "SRP", "PRJNA")):
        return {"error": "Invalid accession format"}

    metadata = await fetch_sra_metadata(accession)
    return metadata


@router.post("/{submission_id}/submit")
async def submit_to_hpc(
    submission_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Submit the job to HPC for processing."""
    # Get submission
    stmt = select(Submission).where(
        Submission.id == submission_id,
        Submission.user_id == user.id,
    )
    result = await db.execute(stmt)
    submission = result.scalar_one_or_none()

    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")

    if submission.status != SubmissionStatus.DRAFT:
        raise HTTPException(status_code=400, detail="Submission already submitted")

    # Submit to SLURM
    if settings.slurm_enabled:
        try:
            job_id = await submit_pipeline_job(submission)
            submission.slurm_job_id = job_id
            submission.status = SubmissionStatus.QUEUED
        except Exception as e:
            submission.status = SubmissionStatus.FAILED
            submission.error_message = str(e)
    else:
        # Dev mode - skip SLURM
        submission.status = SubmissionStatus.SUBMITTED

    submission.submitted_at = datetime.utcnow()
    await db.commit()

    return RedirectResponse(url=f"/submissions/{submission_id}", status_code=302)


@router.get("/{submission_id}/status")
async def get_status(
    submission_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Get current submission status (for polling)."""
    stmt = select(Submission).where(
        Submission.id == submission_id,
        Submission.user_id == user.id,
    )
    result = await db.execute(stmt)
    submission = result.scalar_one_or_none()

    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")

    return {
        "id": submission.id,
        "status": submission.status.value,
        "slurm_job_id": submission.slurm_job_id,
        "github_repo": submission.github_repo,
        "error_message": submission.error_message,
    }


@router.get("")
async def list_submissions(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """List all submissions for current user."""
    stmt = select(Submission).where(Submission.user_id == user.id).order_by(Submission.created_at.desc())
    result = await db.execute(stmt)
    submissions = result.scalars().all()

    return [
        {
            "id": s.id,
            "sra_accession": s.sra_accession,
            "pipeline": s.pipeline.value,
            "title": s.title,
            "status": s.status.value,
            "created_at": s.created_at.isoformat(),
            "github_repo": s.github_repo,
        }
        for s in submissions
    ]
