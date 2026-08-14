"""Submission routes and logic."""
from fastapi import APIRouter, Request, Depends, HTTPException, Form, BackgroundTasks
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import attributes
from datetime import datetime
from typing import Optional

from .config import get_settings
from .database import get_db, Submission, User, SubmissionStatus, PipelineType
from .auth import require_user
from .slurm import submit_local_download_job, write_cluster_marker
from .sra_metadata import fetch_sra_metadata, resolve_to_bioproject

router = APIRouter(prefix="/submissions", tags=["submissions"])
settings = get_settings()


async def _get_submission(slug: str, user: User, db: AsyncSession) -> Submission:
    """Look up a submission by slug, scoped to the current user."""
    stmt = select(Submission).where(
        Submission.slug == slug,
        Submission.user_id == user.id,
        Submission.deleted_at.is_(None),
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
        return PipelineType.ILLUMINA_AMPLICON
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
    if not accession.startswith(("SRR", "ERR", "DRR", "SRX", "SRP", "ERS", "PRJNA", "PRJEB", "PRJDB", "SAMN", "SAME", "SAMD", "SAMEA")):
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
    if not accession.startswith(("SRR", "ERR", "DRR", "SRX", "SRP", "ERS", "PRJNA", "PRJEB", "PRJDB", "SAMN", "SAME", "SAMD", "SAMEA")):
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


def _run_accession(run) -> Optional[str]:
    """Pull an SRA run accession out of a selected_runs entry (str or dict)."""
    if isinstance(run, str):
        return run
    if isinstance(run, dict):
        return run.get("accession") or run.get("run_accession") or run.get("run")
    return None


async def _resolve_primers(submission: Submission):
    """Keep the submitter's primers if they gave any, and otherwise set none.

    Manual or automatic, with nothing in between. Whatever OMC records here is
    passed to the pipeline as fact and used in place of the detection it runs
    over the whole dataset, so a guess made here from SRA metadata or from a
    sample of reads is not a helpful default — it is a wrong answer with the
    authority of a stated one. SRA metadata is unreliable in both directions
    (PRJNA1473294 labels every run "16S" though 40 are 18S), and a read sample
    small enough to be cheap is small enough to be misled by spacers, inline
    barcodes or a thin run.

    Leaving primers unset is what makes the pipeline work them out itself, from
    every sample rather than a probe, and record what it actually trimmed with.
    """
    existing = submission.primers or {}
    if existing.get("source") == "manual" and existing.get("fwd") and existing.get("rev"):
        return  # user-specified on the submission sheet — respect it
    # Nothing else to resolve: manual or automatic, with nothing in between.
    # Detecting here from a spread of runs, or reading a primer name out of the
    # metadata, produced primers that were confidently wrong and were then passed
    # to the pipeline as fact, in place of the detection it does over the whole
    # dataset. Leaving primers unset is what makes it do that.
    submission.primers = None


async def _launch_download(slug: str):
    """Background task: launch the local download and update DB with job ID."""
    from .database import async_session
    async with async_session() as db:
        stmt = select(Submission).where(Submission.slug == slug, Submission.deleted_at.is_(None))
        result = await db.execute(stmt)
        submission = result.scalar_one_or_none()
        if not submission:
            return  # deleted before download started
        try:
            # Resolve amplicon primers before the pipeline script is built.
            if submission.pipeline == PipelineType.ILLUMINA_AMPLICON:
                await _resolve_primers(submission)
                await db.commit()
            job_id = await submit_local_download_job(submission)
            submission.slurm_job_id = job_id
            await db.commit()
        except Exception as e:
            submission.status = SubmissionStatus.FAILED
            submission.error_message = str(e)
            await db.commit()


@router.post("/{slug}/submit")
async def submit_to_hpc(
    slug: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Submit the job to HPC for processing."""
    submission = await _get_submission(slug, user, db)

    if submission.status != SubmissionStatus.DRAFT:
        return RedirectResponse(url=f"/submissions/{slug}", status_code=302)

    if not submission.bioproject_accession:
        raise HTTPException(status_code=400, detail="No accession linked yet")

    if not submission.selected_runs:
        raise HTTPException(status_code=400, detail="No data types selected")

    # Mark as queued and commit immediately so the redirect shows the right status
    submission.status = SubmissionStatus.QUEUED if settings.slurm_enabled else SubmissionStatus.SUBMITTED
    submission.submitted_at = datetime.utcnow()
    await db.commit()

    # Launch download in the background — response returns instantly
    if settings.slurm_enabled:
        background_tasks.add_task(_launch_download, slug)

    return RedirectResponse(url=f"/submissions/{slug}", status_code=302)


@router.get("/{slug}/status")
async def get_status(
    slug: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Get current submission status (for htmx polling).

    Checks HPC marker files for real-time status, updates DB, returns HTML.
    """
    from fastapi.responses import HTMLResponse
    from .slurm import get_submission_status

    submission = await _get_submission(slug, user, db)
    s = submission.status.value

    # For active jobs, check HPC for real status
    if s in ("queued", "submitted", "running") and settings.slurm_enabled:
        try:
            hpc_status = await get_submission_status(submission.slug)
            phase = hpc_status.get("phase", "unknown")
            job_id = hpc_status.get("job_id")

            # Update DB job ID if we got a real one from the download wrapper
            if job_id and submission.slurm_job_id != job_id:
                submission.slurm_job_id = job_id
                await db.commit()

            # Update DB status based on HPC phase
            if phase == "running" and s != "running":
                submission.status = SubmissionStatus.RUNNING
                await db.commit()
                s = "running"
            elif phase == "completed":
                submission.status = SubmissionStatus.PROCESSING
                await db.commit()
                s = "processing"
            elif phase == "transferred":
                # Do NOT set RESULTS_READY here. The finalization for a transferred
                # run — empty-archive → FAILED guard, results_format, and microscape
                # viz deployment — lives only in poll_all_running_jobs, which selects
                # PROCESSING rows. If this fast page poll set RESULTS_READY first, the
                # background poller would never select the row and that finalization
                # (validation + deploy) would be skipped (issue #31). Advance only to
                # PROCESSING and let the authoritative poller finalize.
                submission.status = SubmissionStatus.PROCESSING
                await db.commit()
                s = "processing"
            elif phase == "failed":
                submission.status = SubmissionStatus.FAILED
                submission.error_message = hpc_status.get("reason", f"Exit code {hpc_status.get('exit_code', '?')}")
                await db.commit()
                s = "failed"

            if phase == "downloading":
                detail = hpc_status.get("detail", "")
                if detail == "Queued for download":
                    html = '<p class="status-polling">Queued for download... checking automatically.</p>'
                elif detail == "Waiting for HPC pickup":
                    html = '<p class="status-polling">Download complete. Waiting for HPC to pick up data... checking automatically.</p>'
                elif detail == "Transferring to HPC":
                    html = '<p class="status-polling">Transferring data to HPC... checking automatically.</p>'
                else:
                    html = '<p class="status-polling">Downloading SRA data... checking automatically.</p>'
            elif phase == "queued":
                html = f'<p class="status-polling">Download complete. Pipeline job <strong>queued</strong>'
                if job_id:
                    html += f" (SLURM {job_id})"
                html += "... checking automatically.</p>"
            elif phase == "running":
                html = f'<p class="status-polling">Pipeline is <strong>running</strong>'
                if job_id:
                    html += f" (SLURM {job_id})"
                html += "... checking automatically.</p>"
            elif phase == "completed":
                html = '<p class="status-polling">Pipeline complete. Ready for manuscript generation.</p>'
                html += '<script>setTimeout(() => location.reload(), 1000);</script>'
            elif phase == "failed":
                reason = hpc_status.get("reason", f"Exit code {hpc_status.get('exit_code', '?')}")
                html = f'<p class="status-polling" style="color:var(--color-error)"><strong>Failed:</strong> {reason}</p>'
                html += '<script>setTimeout(() => location.reload(), 1000);</script>'
            else:
                html = '<p class="status-polling">Initializing... checking automatically.</p>'

            return HTMLResponse(html)
        except Exception as e:
            logger.warning(f"HPC status check failed for {slug}: {e}")

    # Fallback for non-HPC or check failure
    job_id = submission.slurm_job_id
    if s in ("queued", "submitted"):
        html = f'<p class="status-polling">Job is <strong>queued</strong>'
        if job_id:
            html += f" (SLURM {job_id})"
        html += "... checking automatically.</p>"
    elif s == "running":
        html = f'<p class="status-polling">Job is <strong>running</strong>'
        if job_id:
            html += f" (SLURM {job_id})"
        html += "... checking automatically.</p>"
    elif s == "failed":
        msg = submission.error_message or "Unknown error"
        html = f'<p class="status-polling" style="color:var(--color-error)"><strong>Failed:</strong> {msg}</p>'
    elif s == "processing":
        html = '<p class="status-polling">Pipeline complete. Ready for manuscript generation.</p>'
    else:
        html = f'<p class="status-polling">Status: <strong>{s}</strong></p>'

    if s not in ("queued", "submitted", "running"):
        html += '<script>setTimeout(() => location.reload(), 1000);</script>'

    return HTMLResponse(html)


@router.post("/{slug}/primers")
async def set_primers(
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Set (or clear) manual amplicon primers on an amplicon submission.

    Blank fields clear manual primers, re-enabling metadata/inferred resolution
    at submit time. Sequences are validated as IUPAC nucleotide strings.
    """
    import re
    submission = await _get_submission(slug, user, db)

    # Accepts repeated fwd/rev fields — a BioProject can legitimately mix
    # amplicon targets (e.g. 16S and 18S), so the author can enter one pair
    # per target rather than being forced to pick a single one.
    form = await request.form()
    fwds = [v.strip().upper() for v in form.getlist("fwd")]
    revs = [v.strip().upper() for v in form.getlist("rev")]

    pairs = []
    for f, r in zip(fwds, revs):
        if not f and not r:
            continue  # blank row — ignore
        if not re.fullmatch(r"[ACGTRYSWKMBDHVN]{10,40}", f) or \
           not re.fullmatch(r"[ACGTRYSWKMBDHVN]{10,40}", r):
            raise HTTPException(400, "Primers must be IUPAC nucleotide sequences (10–40 bp)")
        pairs.append({
            "fwd": f, "rev": r, "fwd_name": "manual", "rev_name": "manual",
            "region": "", "source": "manual", "confidence": None,
        })

    if not pairs:
        submission.primers = None
    else:
        primary = dict(pairs[0])
        if len(pairs) > 1:
            primary["sets"] = pairs
        submission.primers = primary
    attributes.flag_modified(submission, "primers")
    await db.commit()
    return RedirectResponse(url=f"/submissions/{slug}", status_code=302)


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

    # Target cluster — which HPC picks this run up. Admin-only, and only until a
    # cluster has actually claimed the run: once the pipeline is submitted the
    # data and the job live on that cluster and moving them is not a form field.
    if "target_cluster" in body:
        from .auth import is_admin
        if not is_admin(user):
            return JSONResponse({"error": "Admins only"}, status_code=403)
        if submission.status not in (SubmissionStatus.DRAFT, SubmissionStatus.QUEUED):
            return JSONResponse(
                {"error": "Cluster is fixed once the run has been picked up"},
                status_code=400,
            )
        name = (body["target_cluster"] or "").strip()[:32]
        submission.target_cluster = name or None
        # A run already staged for pickup carries its own marker; keep it in step
        # so a retarget between submit and pickup reaches the right cluster.
        write_cluster_marker(submission)

    # Fields locked after HPC submission
    if submission.status == SubmissionStatus.DRAFT:
        if "pipeline" in body:
            try:
                submission.pipeline = PipelineType(body["pipeline"])
            except ValueError:
                return JSONResponse({"error": "Invalid pipeline"}, status_code=400)

        if "sra_accession" in body:
            accession = body["sra_accession"].strip().upper()
            if not accession.startswith(("SRR", "ERR", "DRR", "SRX", "SRP", "ERS", "PRJNA", "PRJEB", "PRJDB", "SAMN", "SAME", "SAMD", "SAMEA")):
                return JSONResponse({"error": "Invalid accession format"}, status_code=400)

            project_metadata = await resolve_to_bioproject(accession)
            bioproject_acc = project_metadata.get("accession", accession)

            submission.bioproject_accession = bioproject_acc
            submission.sra_accession = accession if accession != bioproject_acc else None

            # Fetch per-sample MIxS/MIMARKS metadata from EBI/ENA
            from .sra_metadata import fetch_sample_metadata
            sample_records = await fetch_sample_metadata(bioproject_acc)
            if sample_records:
                project_metadata["sample_records"] = sample_records

            submission.sample_metadata = project_metadata

            # Auto-update title from metadata
            new_title = (
                project_metadata.get("study_title")
                or project_metadata.get("title")
                or f"Analysis of {bioproject_acc}"
            )
            submission.title = new_title

            # Start with nothing selected — user picks data types
            submission.selected_runs = None

            # Auto-pick pipeline from full breakdown (user can change later)
            breakdown = project_metadata.get("breakdown", [])
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

        if "selected_run_accessions" in body:
            # Store individual run accessions chosen in the run selector
            accessions = body["selected_run_accessions"]
            if isinstance(accessions, list):
                # Merge into selected_runs — keep type dicts plus run accession list
                sr = submission.selected_runs or []
                # Store as mixed list: type dicts + accession strings
                # The download worker reads run_accessions from the type dicts,
                # but if individual accessions are provided, use those instead
                interview_data = dict(submission.interview_data or {})
                interview_data["_selected_run_accessions"] = accessions
                submission.interview_data = interview_data
                attributes.flag_modified(submission, "interview_data")

    # Manuscript section edits (from preview page)
    if "manuscript_section" in body and "manuscript_text" in body:
        section_name = body["manuscript_section"]
        section_text = body["manuscript_text"]
        interview_data = dict(submission.interview_data or {})
        manuscript = dict(interview_data.get("_manuscript", {}))
        if section_name in ("abstract", "introduction", "methods", "results", "discussion", "bibliography"):
            manuscript[section_name] = section_text
            interview_data["_manuscript"] = manuscript
            submission.interview_data = interview_data
            attributes.flag_modified(submission, "interview_data")

    await db.commit()
    return JSONResponse({"ok": True, "title": submission.title, "pipeline": submission.pipeline.value})


@router.post("/{slug}/delete")
async def delete_submission(
    slug: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Soft-delete a submission. Cleanup cron removes files later."""
    submission = await _get_submission(slug, user, db)

    submission.deleted_at = datetime.utcnow()
    await db.commit()

    return RedirectResponse(url="/dashboard", status_code=302)


@router.get("")
async def list_submissions(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """List all submissions for current user."""
    stmt = select(Submission).where(Submission.user_id == user.id, Submission.deleted_at.is_(None)).order_by(Submission.created_at.desc())
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
