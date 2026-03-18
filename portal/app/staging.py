"""SRA download staging API.

Arbutus downloads SRA data locally, stages it, and exposes it over HTTP.
A cron job on fir polls for ready downloads, fetches files, and submits
pipeline jobs — no SSH required from either side.

Fir also pushes status updates back via POST /staging/{slug}/status,
so the portal never needs to SSH to fir for status checks.

Endpoints are authenticated with the staging API key.
"""
import json
import os
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Header, Request
from fastapi.responses import FileResponse, JSONResponse

from .config import get_settings

router = APIRouter(prefix="/staging", tags=["staging"])
settings = get_settings()
logger = logging.getLogger(__name__)


def _check_staging_key(authorization: str = Header(default="")) -> None:
    """Verify Bearer token matches the staging/relay API key."""
    if not settings.staging_api_key:
        raise HTTPException(503, "Staging API not configured")
    token = authorization.removeprefix("Bearer ").strip()
    if token != settings.staging_api_key:
        raise HTTPException(401, "Invalid staging key")


@router.get("/ready")
async def list_ready(authorization: str = Header(default="")):
    """List slugs that have finished downloading and are ready for pickup.

    Returns a list of objects with slug and file manifest.
    """
    _check_staging_key(authorization)

    staging_root = Path(settings.local_download_path)
    if not staging_root.exists():
        return {"ready": []}

    ready = []
    for slug_dir in staging_root.iterdir():
        if not slug_dir.is_dir():
            continue
        ready_marker = slug_dir / ".ready"
        if not ready_marker.exists():
            continue

        # Build file manifest
        fastq_dir = slug_dir / "fastq"
        files = []
        if fastq_dir.exists():
            files = [f.name for f in fastq_dir.iterdir() if f.is_file()]

        # Check for pipeline.sh
        has_pipeline = (slug_dir / "pipeline.sh").exists()

        ready.append({
            "slug": slug_dir.name,
            "files": sorted(files),
            "has_pipeline": has_pipeline,
            "ready_at": ready_marker.read_text().strip(),
        })

    return {"ready": ready}


@router.get("/{slug}/files")
async def list_files(slug: str, authorization: str = Header(default="")):
    """List downloadable files for a staged submission."""
    _check_staging_key(authorization)

    slug_dir = Path(settings.local_download_path) / slug
    if not slug_dir.exists() or not (slug_dir / ".ready").exists():
        raise HTTPException(404, "Slug not found or not ready")

    files = []
    # pipeline.sh at top level
    pipeline = slug_dir / "pipeline.sh"
    if pipeline.exists():
        files.append({"name": "pipeline.sh", "size": pipeline.stat().st_size, "path": "pipeline.sh"})

    # fastq files
    fastq_dir = slug_dir / "fastq"
    if fastq_dir.exists():
        for f in sorted(fastq_dir.iterdir()):
            if f.is_file():
                files.append({"name": f.name, "size": f.stat().st_size, "path": f"fastq/{f.name}"})

    return {"slug": slug, "files": files}


@router.get("/{slug}/download/{file_path:path}")
async def download_file(slug: str, file_path: str, authorization: str = Header(default="")):
    """Download a specific file from the staging area."""
    _check_staging_key(authorization)

    slug_dir = Path(settings.local_download_path) / slug
    if not slug_dir.exists():
        raise HTTPException(404, "Slug not found")

    target = (slug_dir / file_path).resolve()
    # Prevent path traversal
    if not str(target).startswith(str(slug_dir.resolve())):
        raise HTTPException(403, "Invalid path")
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "File not found")

    return FileResponse(target, filename=target.name)


@router.post("/{slug}/picked-up")
async def mark_picked_up(slug: str, authorization: str = Header(default="")):
    """Mark a staged download as picked up by fir. Cleans up local staging."""
    _check_staging_key(authorization)

    slug_dir = Path(settings.local_download_path) / slug
    if not slug_dir.exists():
        raise HTTPException(404, "Slug not found")

    import shutil
    shutil.rmtree(slug_dir, ignore_errors=True)
    logger.info(f"Staging cleaned up for {slug}")

    return {"ok": True, "slug": slug}


# ── Per-run streaming pickup ────────────────────────────────────────────


@router.get("/ready-runs")
async def list_ready_runs(authorization: str = Header(default="")):
    """List individual runs that have finished downloading and are ready for pickup.

    Returns runs grouped by slug. Fir can pick up runs incrementally
    as they complete, rather than waiting for the entire job to finish.
    """
    _check_staging_key(authorization)

    staging_root = Path(settings.local_download_path)
    if not staging_root.exists():
        return {"ready_runs": []}

    ready_runs = []
    for slug_dir in staging_root.iterdir():
        if not slug_dir.is_dir() or slug_dir.name.startswith("."):
            continue

        run_ready_dir = slug_dir / ".run-ready"
        if not run_ready_dir.exists():
            continue

        runs = []
        for marker in run_ready_dir.iterdir():
            if not marker.is_file():
                continue
            acc = marker.name
            # Check the run hasn't already been picked up
            picked_up_dir = slug_dir / ".run-picked-up"
            if (picked_up_dir / acc).exists():
                continue
            # Find matching fastq files
            fastq_dir = slug_dir / "fastq"
            files = []
            if fastq_dir.exists():
                files = [f.name for f in fastq_dir.iterdir()
                         if f.is_file() and f.name.startswith(acc)]
            if files:
                runs.append({
                    "accession": acc,
                    "files": sorted(files),
                    "ready_at": marker.read_text().strip(),
                })

        if runs:
            # Also report whether the whole job is done
            all_done = (slug_dir / ".ready").exists()
            has_pipeline = (slug_dir / "pipeline.sh").exists()
            ready_runs.append({
                "slug": slug_dir.name,
                "runs": runs,
                "all_done": all_done,
                "has_pipeline": has_pipeline,
            })

    return {"ready_runs": ready_runs}


@router.post("/{slug}/run-picked-up/{accession}")
async def mark_run_picked_up(slug: str, accession: str, authorization: str = Header(default="")):
    """Mark a single run as picked up by fir. Deletes local fastq files for that run."""
    _check_staging_key(authorization)

    slug_dir = Path(settings.local_download_path) / slug
    if not slug_dir.exists():
        raise HTTPException(404, "Slug not found")

    # Mark as picked up
    picked_up_dir = slug_dir / ".run-picked-up"
    picked_up_dir.mkdir(exist_ok=True)
    (picked_up_dir / accession).write_text(
        __import__("datetime").datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    # Delete local fastq files for this run to free disk
    fastq_dir = slug_dir / "fastq"
    if fastq_dir.exists():
        deleted = []
        for f in fastq_dir.iterdir():
            if f.is_file() and f.name.startswith(accession):
                f.unlink()
                deleted.append(f.name)
        if deleted:
            logger.info(f"Freed {len(deleted)} files for {slug}/{accession}: {deleted}")

    return {"ok": True, "slug": slug, "accession": accession}


# ── Status push (fir → arbutus) ─────────────────────────────────────────

# Status files live alongside the staging area
_STATUS_DIR = Path(settings.local_download_path) / ".hpc_status"


@router.post("/{slug}/status")
async def push_status(slug: str, request: Request, authorization: str = Header(default="")):
    """Receive a status update from fir (cron or pipeline wrapper).

    Body: {"phase": "...", "job_id": "...", "exit_code": "...", ...}
    """
    _check_staging_key(authorization)

    _STATUS_DIR.mkdir(parents=True, exist_ok=True)
    body = await request.json()
    status_file = _STATUS_DIR / f"{slug}.json"
    status_file.write_text(json.dumps(body))
    logger.info(f"HPC status update for {slug}: {body.get('phase', '?')}")

    return {"ok": True}


# ── Squashfs results upload (fir → arbutus) ─────────────────────────────

# Results .sqsh files stored separately from download staging
_RESULTS_DIR = Path(settings.local_download_path).parent / "results"


@router.post("/{slug}/upload-results")
async def upload_results(slug: str, request: Request, authorization: str = Header(default="")):
    """Receive a squashfs results archive from fir.

    Fir streams the .sqsh file as the request body after pipeline completion.
    Stored at {results_dir}/{slug}.sqsh for squashfuse mounting into session containers.
    """
    _check_staging_key(authorization)

    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    sqsh_path = _RESULTS_DIR / f"{slug}.sqsh"

    # Stream the upload to disk to handle large files.
    # IMPORTANT: f.write() is blocking — must use asyncio.to_thread() to avoid
    # buffering the entire request body in memory. Without this, uvicorn accepts
    # data from nginx faster than disk writes complete, causing OOM on large uploads.
    import asyncio
    bytes_written = 0
    with open(sqsh_path, "wb") as f:
        async for chunk in request.stream():
            await asyncio.to_thread(f.write, chunk)
            bytes_written += len(chunk)

    if bytes_written == 0:
        sqsh_path.unlink(missing_ok=True)
        raise HTTPException(400, "Empty upload")

    size_mb = bytes_written / 1_000_000
    logger.info(f"Received results archive for {slug}: {sqsh_path.name} ({size_mb:.1f} MB)")

    return {"ok": True, "slug": slug, "file": sqsh_path.name, "size_bytes": bytes_written}


@router.get("/results")
async def list_results(authorization: str = Header(default="")):
    """List available squashfs result archives."""
    _check_staging_key(authorization)

    if not _RESULTS_DIR.exists():
        return {"results": []}

    results = []
    for f in sorted(_RESULTS_DIR.iterdir()):
        if f.suffix == ".sqsh" and f.is_file():
            results.append({
                "slug": f.stem,
                "file": f.name,
                "size_bytes": f.stat().st_size,
            })

    return {"results": results}


@router.delete("/{slug}/results")
async def delete_results(slug: str, authorization: str = Header(default="")):
    """Delete a results archive."""
    _check_staging_key(authorization)

    sqsh_path = _RESULTS_DIR / f"{slug}.sqsh"
    if not sqsh_path.exists():
        raise HTTPException(404, "Results archive not found")

    sqsh_path.unlink()
    logger.info(f"Deleted results archive for {slug}")
    return {"ok": True, "slug": slug}


def get_results_path(slug: str) -> Path | None:
    """Get the path to a slug's results .sqsh file, if it exists."""
    sqsh_path = _RESULTS_DIR / f"{slug}.sqsh"
    return sqsh_path if sqsh_path.exists() else None


def get_hpc_status(slug: str) -> dict | None:
    """Read the latest status pushed by fir for a given slug.

    Returns None if no status has been pushed yet.
    """
    status_file = _STATUS_DIR / f"{slug}.json"
    if status_file.exists():
        try:
            return json.loads(status_file.read_text())
        except (json.JSONDecodeError, OSError):
            return None
    return None
