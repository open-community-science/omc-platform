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
    if not slug_dir.exists() or not (slug_dir / ".ready").exists():
        raise HTTPException(404, "Slug not found or not ready")

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
