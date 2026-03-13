"""Submission routes and logic."""
from fastapi import APIRouter, Request, Depends, HTTPException, Form
from fastapi.responses import RedirectResponse, JSONResponse
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


def _auto_pipeline(breakdown: list) -> PipelineType:
    """Pick a pipeline from breakdown rows (best guess)."""
    if not breakdown:
        return PipelineType.NANOPORE_MAG

    platforms = {b.get("platform", "").upper() for b in breakdown}
    strategies = {b.get("strategy", "").upper() for b in breakdown}
    sources = {b.get("source", "").upper() for b in breakdown}

    has_long = any("OXFORD" in p or "NANOPORE" in p or "PACBIO" in p for p in platforms)
    has_short = any("ILLUMINA" in p or "BGISEQ" in p for p in platforms)

    if any("AMPLICON" in s for s in strategies):
        return PipelineType.MICROSCAPE
    if any("RNA" in s for s in strategies):
        return PipelineType.RNASEQ
    if any("METAGENOMIC" in s or "METATRANSCRIPTOMIC" in s for s in sources):
        return PipelineType.NANOPORE_MAG if has_long else PipelineType.ILLUMINA_MAG
    if any("WGS" in s or "WGA" in s for s in strategies):
        if has_long:
            return PipelineType.NANOPORE_MAG
        return PipelineType.ILLUMINA_MAG
    return PipelineType.NANOPORE_MAG


@router.post("/create")
async def create_submission(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Create a blank draft submission and redirect to its detail page."""
    submission = Submission(
        user_id=user.id,
        title="Untitled Submission",
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

    if not submission.bioproject_accession:
        raise HTTPException(status_code=400, detail="No accession linked yet")

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


@router.post("/{slug}/update")
async def update_submission(
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Update editable submission fields.

    Title is always editable.
    Accession, pipeline, and data selection are only editable in draft.
    """
    import json as _json

    submission = await _get_submission(slug, user, db)
    body = await request.json()

    # Title — always editable
    if "title" in body:
        title = body["title"].strip()
        if title:
            submission.title = title

    # Fields locked after HPC submission
    if submission.status == SubmissionStatus.DRAFT:
        if "pipeline" in body:
            try:
                submission.pipeline = PipelineType(body["pipeline"])
            except ValueError:
                return JSONResponse({"error": "Invalid pipeline"}, status_code=400)

        if "sra_accession" in body:
            accession = body["sra_accession"].strip().upper()
            if not accession.startswith(("SRR", "ERR", "DRR", "SRX", "SRP", "PRJNA", "SAMN", "SAME", "SAMD")):
                return JSONResponse({"error": "Invalid accession format"}, status_code=400)

            project_metadata = await resolve_to_bioproject(accession)
            bioproject_acc = project_metadata.get("accession", accession)

            submission.bioproject_accession = bioproject_acc
            submission.sra_accession = accession if accession != bioproject_acc else None
            submission.sample_metadata = project_metadata

            # Auto-update title from metadata
            new_title = (
                project_metadata.get("study_title")
                or project_metadata.get("title")
                or f"Analysis of {bioproject_acc}"
            )
            submission.title = new_title

            # Re-select all breakdown rows by default
            breakdown = project_metadata.get("breakdown", [])
            submission.selected_runs = breakdown if breakdown else None

            # Auto-pick pipeline for new accession
            submission.pipeline = _auto_pipeline(breakdown)

        if "selected_runs" in body:
            # Accept list of breakdown indices → store the actual row dicts
            breakdown = (submission.sample_metadata or {}).get("breakdown", [])
            indices = body["selected_runs"]
            if isinstance(indices, list) and breakdown:
                submission.selected_runs = [breakdown[i] for i in indices if isinstance(i, int) and i < len(breakdown)]
                # Re-auto pipeline from selected subset
                submission.pipeline = _auto_pipeline(submission.selected_runs)
            elif not indices:
                submission.selected_runs = None

    await db.commit()
    return JSONResponse({"ok": True, "title": submission.title, "pipeline": submission.pipeline.value})


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
