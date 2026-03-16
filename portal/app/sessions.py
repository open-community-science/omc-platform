"""Session manager — spins up isolated Docker containers for author sessions.

Each submission gets its own container running Chainlit + Marimo with:
- Dataset mounted read-only
- LLM access via portal proxy only (no direct network)
- Unique port allocation
- Container stopped (not removed) when idle, resumable later
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pathlib import Path

from .config import get_settings
from .database import get_db, async_session, Submission, User
from .auth import require_user
from .llm_proxy import create_session_token, revoke_session_token
from .staging import get_results_path

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sessions", tags=["sessions"])
settings = get_settings()

BASE_DIR = Path(__file__).parent.parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# Port range for session containers
PORT_RANGE_START = 9100
PORT_RANGE_END = 9200

SESSION_IMAGE = "omc-session:latest"
SESSION_NETWORK = "omc-sessions"
GATEWAY_IP = "172.30.0.1"  # host gateway on the session network
PORTAL_PORT = 8002

DATA_BASE_PATH = Path(settings.local_download_path) if hasattr(settings, "local_download_path") else Path("/data/sra_downloads")
SQSH_MOUNT_BASE = Path("/mnt/omc-sessions")  # host mountpoints for squashfuse


@dataclass
class SessionInfo:
    slug: str
    container_id: str
    chat_port: int
    notebook_port: int
    session_token: str = ""
    data_mount: str = ""  # host path to bind-mount as /data (dir or squashfuse mountpoint)
    sqsh_mounted: bool = False  # True if we squashfuse-mounted a .sqsh
    started_at: datetime = field(default_factory=datetime.utcnow)
    status: str = "running"  # running, stopped


# In-memory session registry (replace with DB/Redis for production)
_sessions: dict[str, SessionInfo] = {}
_used_ports: set[int] = set()


def _allocate_ports() -> tuple[int, int]:
    """Allocate two consecutive ports for chat and notebook."""
    for base in range(PORT_RANGE_START, PORT_RANGE_END, 2):
        if base not in _used_ports and (base + 1) not in _used_ports:
            _used_ports.add(base)
            _used_ports.add(base + 1)
            return base, base + 1
    raise RuntimeError("No available ports for new session")


def _release_ports(chat_port: int, notebook_port: int):
    _used_ports.discard(chat_port)
    _used_ports.discard(notebook_port)


async def _run_docker(cmd: list[str]) -> str:
    """Run a docker command and return stdout."""
    proc = await asyncio.create_subprocess_exec(
        "docker", *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"docker {cmd[0]} failed: {stderr.decode()}")
    return stdout.decode().strip()


async def _sqsh_mount(slug: str, sqsh_path: Path) -> Path:
    """Mount a .sqsh file via squashfuse, return the mountpoint."""
    mountpoint = SQSH_MOUNT_BASE / slug

    # Clean up stale FUSE mountpoints (transport endpoint not connected)
    if mountpoint.exists():
        try:
            list(mountpoint.iterdir())  # test if mount is alive
            return mountpoint  # already mounted and working
        except OSError:
            # Stale mount — force unmount
            await asyncio.create_subprocess_exec(
                "fusermount", "-u", str(mountpoint),
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                mountpoint.rmdir()
            except OSError:
                pass

    mountpoint.mkdir(parents=True, exist_ok=True)

    proc = await asyncio.create_subprocess_exec(
        "squashfuse", "-o", "allow_other", str(sqsh_path), str(mountpoint),
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"squashfuse failed: {stderr.decode()}")
    logger.info(f"Mounted {sqsh_path.name} at {mountpoint}")
    return mountpoint


async def _sqsh_unmount(slug: str):
    """Unmount a squashfuse mountpoint."""
    mountpoint = SQSH_MOUNT_BASE / slug
    if not mountpoint.exists():
        return
    proc = await asyncio.create_subprocess_exec(
        "fusermount", "-u", str(mountpoint),
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    try:
        mountpoint.rmdir()
    except OSError:
        pass
    logger.info(f"Unmounted {mountpoint}")


async def _ensure_network():
    """Create the session network if it doesn't exist."""
    try:
        await _run_docker(["network", "inspect", SESSION_NETWORK])
    except RuntimeError:
        await _run_docker([
            "network", "create",
            "--driver", "bridge",
            "--subnet", "172.30.0.0/24",
            "--gateway", GATEWAY_IP,
            "--opt", "com.docker.network.bridge.enable_icc=false",
            "--opt", "com.docker.network.bridge.enable_ip_masquerade=false",
            SESSION_NETWORK,
        ])
        logger.info(f"Created Docker network {SESSION_NETWORK}")


async def launch_session(slug: str, metadata: dict) -> SessionInfo:
    """Launch a new session container for a submission."""
    if slug in _sessions and _sessions[slug].status == "running":
        return _sessions[slug]

    # Check for stopped container to resume
    if slug in _sessions and _sessions[slug].status == "stopped":
        return await resume_session(slug)

    await _ensure_network()

    # Check for orphaned container from a previous portal process
    # Only remove if we don't have it tracked in _sessions
    container_name = f"omc-session-{slug}"
    if slug not in _sessions:
        try:
            # Check if container exists (inspect returns 0 if it does)
            await _run_docker(["inspect", container_name])
            # It exists but we don't track it — remove it
            await _run_docker(["rm", "-f", container_name])
            logger.info(f"Removed orphaned container {container_name}")
        except RuntimeError:
            pass  # doesn't exist, good

    chat_port, nb_port = _allocate_ports()
    data_path = DATA_BASE_PATH / slug

    # Generate a session-scoped token for the LLM proxy
    session_token = create_session_token(slug)

    # Container talks to portal at GATEWAY_IP:PORTAL_PORT
    proxy_base_url = f"http://{GATEWAY_IP}:{PORTAL_PORT}/api/llm"

    # Resolve data source: prefer .sqsh archive, fall back to loose files
    data_mount = ""
    sqsh_mounted = False
    sqsh_path = get_results_path(slug)
    if sqsh_path:
        # Mount squashfs archive via squashfuse on the host
        try:
            mountpoint = await _sqsh_mount(slug, sqsh_path)
            data_mount = str(mountpoint)
            sqsh_mounted = True
        except RuntimeError as e:
            logger.warning(f"squashfuse mount failed for {slug}, falling back to loose files: {e}")
    if not data_mount and data_path.exists():
        data_mount = str(data_path)

    container_name = f"omc-session-{slug}"
    cmd = [
        "run", "-d",
        "--name", container_name,
        "--network", SESSION_NETWORK,
        "-p", f"127.0.0.1:{chat_port}:8080",
        "-p", f"127.0.0.1:{nb_port}:8081",
        # LLM goes through the portal proxy — no direct access
        "-e", f"LLM_BASE_URL={proxy_base_url}",
        "-e", f"LLM_API_KEY={session_token}",
        "-e", f"LLM_MODEL={settings.llm_model}",
        "-e", f"SUBMISSION_META={json.dumps(metadata)}",
        "-e", f"MARIMO_URL=http://localhost:{nb_port}",
        "-e", f"CHAT_ROOT_PATH=/session-proxy/{chat_port}",
        "-e", f"NB_ROOT_PATH=/session-proxy/{nb_port}",
        "--memory", "2g",
        "--cpus", "1.0",
    ]

    # Mount data (squashfuse mountpoint or loose directory) read-only
    if data_mount:
        cmd.extend(["-v", f"{data_mount}:/data:ro"])

    cmd.append(SESSION_IMAGE)

    try:
        container_id = await _run_docker(cmd)
    except RuntimeError as e:
        _release_ports(chat_port, nb_port)
        revoke_session_token(slug)
        raise HTTPException(500, f"Failed to launch session: {e}")

    session = SessionInfo(
        slug=slug,
        container_id=container_id[:12],
        chat_port=chat_port,
        notebook_port=nb_port,
        session_token=session_token,
        data_mount=data_mount,
        sqsh_mounted=sqsh_mounted,
    )
    _sessions[slug] = session
    logger.info(f"Launched session {slug} → container {container_id[:12]} "
                f"(chat:{chat_port}, nb:{nb_port}, network:{SESSION_NETWORK})")
    return session


async def stop_session(slug: str):
    """Stop (but don't remove) a session container."""
    if slug not in _sessions:
        return
    session = _sessions[slug]
    try:
        await _run_docker(["stop", f"omc-session-{slug}"])
        session.status = "stopped"
        logger.info(f"Stopped session {slug}")
    except RuntimeError:
        pass


async def resume_session(slug: str) -> SessionInfo:
    """Resume a stopped session container."""
    if slug not in _sessions:
        raise HTTPException(404, "No session found")
    session = _sessions[slug]
    try:
        await _run_docker(["start", f"omc-session-{slug}"])
        session.status = "running"
        logger.info(f"Resumed session {slug}")
    except RuntimeError as e:
        raise HTTPException(500, f"Failed to resume session: {e}")
    return session


async def remove_session(slug: str):
    """Fully remove a session container and free ports."""
    if slug not in _sessions:
        return
    session = _sessions[slug]
    try:
        await _run_docker(["rm", "-f", f"omc-session-{slug}"])
    except RuntimeError:
        pass
    # Unmount squashfuse if we mounted it
    if session.sqsh_mounted:
        await _sqsh_unmount(slug)
    _release_ports(session.chat_port, session.notebook_port)
    revoke_session_token(slug)
    del _sessions[slug]
    logger.info(f"Removed session {slug}")


# ── Routes ───────────────────────────────────────────────────────────────────

@router.post("/{slug}/launch")
async def launch(
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Launch or resume a session for a submission."""
    stmt = select(Submission).where(
        Submission.slug == slug,
        Submission.user_id == user.id,
    )
    result = await db.execute(stmt)
    submission = result.scalar_one_or_none()
    if not submission:
        raise HTTPException(404, "Submission not found")

    # Light metadata for env var — full metadata is in /data/metadata.json
    metadata = {
        "accession": submission.bioproject_accession,
        "pipeline": submission.pipeline.value,
        "title": submission.title or "",
    }

    session = await launch_session(slug, metadata)

    # If called from a form (browser), redirect to the chat UI
    # If called from JS/API, return JSON
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        return RedirectResponse(f"/sessions/{slug}/chat", status_code=303)

    return {
        "slug": slug,
        "status": session.status,
        "chat_url": f"/sessions/{slug}/chat",
        "notebook_url": f"/sessions/{slug}/notebook",
    }


@router.get("/{slug}/chat", response_class=HTMLResponse)
async def session_chat(
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Serve the Chainlit chat UI (reverse-proxied from container)."""
    if slug not in _sessions or _sessions[slug].status != "running":
        return templates.TemplateResponse(
            "session_launch.html",
            {"request": request, "user": user, "slug": slug},
        )

    session = _sessions[slug]
    return templates.TemplateResponse(
        "session.html",
        {
            "request": request,
            "user": user,
            "slug": slug,
            "chat_port": session.chat_port,
            "notebook_port": session.notebook_port,
        },
    )


@router.get("/{slug}/notebook", response_class=HTMLResponse)
async def session_notebook(
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Redirect to marimo notebook for this session."""
    if slug not in _sessions or _sessions[slug].status != "running":
        raise HTTPException(404, "Session not running")
    session = _sessions[slug]
    # In production, nginx would reverse-proxy this
    return RedirectResponse(f"http://localhost:{session.notebook_port}")


@router.post("/{slug}/stop")
async def stop(
    slug: str,
    user: User = Depends(require_user),
):
    """Stop a session (can be resumed later)."""
    await stop_session(slug)
    return {"slug": slug, "status": "stopped"}


@router.post("/{slug}/resume")
async def resume(
    slug: str,
    user: User = Depends(require_user),
):
    """Resume a stopped session."""
    session = await resume_session(slug)
    return {
        "slug": slug,
        "status": session.status,
        "chat_url": f"/sessions/{slug}/chat",
    }


@router.get("/")
async def list_sessions(user: User = Depends(require_user)):
    """List all sessions for admin/debug."""
    return {
        slug: {
            "status": s.status,
            "chat_port": s.chat_port,
            "notebook_port": s.notebook_port,
            "started_at": s.started_at.isoformat(),
        }
        for slug, s in _sessions.items()
    }


@router.post("/dev/launch/{slug}")
async def dev_launch(slug: str, request: Request):
    """DEV ONLY: Launch a session without auth. Remove in production."""
    if not settings.debug:
        raise HTTPException(403, "Only available in debug mode")

    metadata = {"accession": "dev-test", "pipeline": "nanopore_mag", "title": "Dev test session"}

    # Try to get real metadata from DB
    async with async_session() as db:
        stmt = select(Submission).where(Submission.slug == slug)
        result = await db.execute(stmt)
        submission = result.scalar_one_or_none()
        if submission:
            metadata = {
                "accession": submission.bioproject_accession,
                "pipeline": submission.pipeline.value,
                "title": submission.title or "",
                "sample_metadata": submission.sample_metadata or {},
                "interview": submission.interview_data or {},
            }

    session = await launch_session(slug, metadata)
    return {
        "slug": slug,
        "status": session.status,
        "chat_port": session.chat_port,
        "notebook_port": session.notebook_port,
        "chat_url": f"/sessions/{slug}/chat",
        "chat_proxy": f"/session-proxy/{session.chat_port}/",
        "notebook_proxy": f"/session-proxy/{session.notebook_port}/",
    }


@router.post("/dev/stop/{slug}")
async def dev_stop(slug: str):
    """DEV ONLY: Stop a session without auth."""
    if not settings.debug:
        raise HTTPException(403, "Only available in debug mode")
    await stop_session(slug)
    return {"slug": slug, "status": "stopped"}


@router.post("/dev/remove/{slug}")
async def dev_remove(slug: str):
    """DEV ONLY: Remove a session without auth."""
    if not settings.debug:
        raise HTTPException(403, "Only available in debug mode")
    await remove_session(slug)
    return {"slug": slug, "status": "removed"}
