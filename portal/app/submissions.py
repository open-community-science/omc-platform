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
from .sra_metadata import fetch_sra_metadata, resolve_to_bioproject

router = APIRouter(prefix="/submissions", tags=["submissions"])
settings = get_settings()


async def _get_submission(slug: str, user: User, db: AsyncSession) -> Submission:
    """Look up a submission by slug, scoped to the current user."""
    stmt = select(Submission).where(
        Submission.slug == slug,
        Submission.user_id == user.id,
    )
    result = await db.execute(stmt)
    submission = result.scalar_one_or_none()
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")
    return submission


@router.post("/create")
async def create_submission(
    request: Request,
    sra_accession: str = Form(...),
    pipeline: str = Form(...),
    title: Optional[str] = Form(None),
    selected_breakdown: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Create a new submission."""
    import json as _json

    # Validate SRA accession format (basic check)
    sra_accession = sra_accession.strip().upper()
    if not (sra_accession.startswith(("SRR", "ERR", "DRR", "SRX", "SRP", "PRJNA", "SAMN", "SAME", "SAMD"))):
        raise HTTPException(status_code=400, detail="Invalid SRA accession format")

    # Validate pipeline
    try:
        pipeline_type = PipelineType(pipeline)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid pipeline selection")

    # Resolve to BioProject (any accession → PRJNA)
    project_metadata = await resolve_to_bioproject(sra_accession)

    bioproject_acc = project_metadata.get("accession", sra_accession)

    # Parse selected breakdown indices and store the selected subset metadata
    selected_data = None
    if selected_breakdown and project_metadata.get("breakdown"):
        try:
            indices = _json.loads(selected_breakdown)
            breakdown = project_metadata["breakdown"]
            selected_data = [breakdown[i] for i in indices if i < len(breakdown)]
        except (ValueError, IndexError):
            pass

    # Use study title as default paper title if available
    default_title = (
        project_metadata.get("study_title")
        or project_metadata.get("title")
        or f"Analysis of {bioproject_acc}"
    )

    # Create submission
    submission = Submission(
        user_id=user.id,
        bioproject_accession=bioproject_acc,
        sra_accession=sra_accession if sra_accession != bioproject_acc else None,
        selected_runs=selected_data,
        pipeline=pipeline_type,
        title=title or default_title,
        sample_metadata=project_metadata,
        status=SubmissionStatus.DRAFT,
    )
    db.add(submission)
    await db.commit()
    await db.refresh(submission)

    return RedirectResponse(url=f"/submissions/{submission.slug}", status_code=302)


@router.get("/lookup/{accession}")
async def lookup_accession(accession: str):
    """Live lookup — resolves any accession to its parent BioProject."""
    accession = accession.strip().upper()
    if not accession.startswith(("SRR", "ERR", "DRR", "SRX", "SRP", "PRJNA", "SAMN", "SAME", "SAMD")):
        return {"error": "Invalid accession format"}

    metadata = await resolve_to_bioproject(accession)
    return metadata


@router.get("/lookup/{accession}/samples")
async def list_samples(accession: str):
    """Return the breakdown of data types and sample counts for a BioProject.

    This powers the sample picker UI — users see the breakdown table
    and select which data types (combinations of platform/strategy/source)
    to include in their analysis.

    Returns:
        breakdown: list of {platform, instrument, strategy, source, layout, runs, samples, bases}
        num_samples: total unique samples
        num_runs: total runs
        organism: detected organism(s)
        suggested_pipeline: auto-selected pipeline based on data types
    """
    accession = accession.strip().upper()
    if not accession.startswith(("SRR", "ERR", "DRR", "SRX", "SRP", "PRJNA", "SAMN", "SAME", "SAMD")):
        return {"error": "Invalid accession format"}

    metadata = await resolve_to_bioproject(accession)
    if "error" in metadata:
        return metadata

    breakdown = metadata.get("breakdown", [])
    num_runs = sum(b["runs"] for b in breakdown) if breakdown else 0

    # Auto-suggest pipeline based on data types
    platforms = {b["platform"].lower() for b in breakdown if b["platform"]}
    strategies = {b["strategy"].lower() for b in breakdown if b["strategy"]}

    suggested = "nanopore_mag"
    if "illumina" in " ".join(platforms).lower():
        if "oxford_nanopore" in " ".join(platforms).lower():
            suggested = "nanopore_mag"  # Hybrid
        else:
            suggested = "illumina_mag"
    if "rna-seq" in strategies or "rna_seq" in strategies:
        suggested = "rnaseq"

    return {
        "accession": metadata.get("accession", accession),
        "title": metadata.get("title", ""),
        "organism": metadata.get("organism", ""),
        "num_samples": metadata.get("num_samples", 0),
        "num_runs": num_runs,
        "breakdown": breakdown,
        "suggested_pipeline": suggested,
        "library_strategy": metadata.get("library_strategy", ""),
        "platform": metadata.get("platform", ""),
    }


@router.post("/{slug}/submit")
async def submit_to_hpc(
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Submit the job to HPC for processing."""
    submission = await _get_submission(slug, user, db)

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

    return RedirectResponse(url=f"/submissions/{slug}", status_code=302)


@router.get("/{slug}/status")
async def get_status(
    slug: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Get current submission status (for polling)."""
    submission = await _get_submission(slug, user, db)

    return {
        "slug": submission.slug,
        "status": submission.status.value,
        "slurm_job_id": submission.slurm_job_id,
        "github_repo": submission.github_repo,
        "error_message": submission.error_message,
    }


@router.post("/{slug}/delete")
async def delete_submission(
    slug: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Delete a submission."""
    submission = await _get_submission(slug, user, db)

    await db.delete(submission)
    await db.commit()

    return RedirectResponse(url="/dashboard", status_code=302)


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
            "slug": s.slug,
            "bioproject_accession": s.bioproject_accession,
            "sra_accession": s.sra_accession,
            "pipeline": s.pipeline.value,
            "title": s.title,
            "status": s.status.value,
            "created_at": s.created_at.isoformat(),
            "github_repo": s.github_repo,
        }
        for s in submissions
    ]
