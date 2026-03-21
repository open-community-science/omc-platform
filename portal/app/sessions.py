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
    viz_port: int = 0
    session_token: str = ""
    data_mount: str = ""  # host path to bind-mount as /data (dir or squashfuse mountpoint)
    sqsh_mounted: bool = False  # True if we squashfuse-mounted a .sqsh
    started_at: datetime = field(default_factory=datetime.utcnow)
    status: str = "running"  # running, stopped
    chat_state: dict | None = None  # cached chat state for resume without git


# In-memory session registry (replace with DB/Redis for production)
_sessions: dict[str, SessionInfo] = {}
_used_ports: set[int] = set()


def _allocate_ports() -> tuple[int, int, int]:
    """Allocate three consecutive ports for chat, notebook, and viz."""
    for base in range(PORT_RANGE_START, PORT_RANGE_END, 3):
        if all(base + i not in _used_ports for i in range(3)):
            for i in range(3):
                _used_ports.add(base + i)
            return base, base + 1, base + 2
    raise RuntimeError("No available ports for new session")


def _release_ports(chat_port: int, notebook_port: int, viz_port: int = 0):
    _used_ports.discard(chat_port)
    _used_ports.discard(notebook_port)
    if viz_port:
        _used_ports.discard(viz_port)


async def _recover_sessions():
    """Recover session state from running Docker containers after portal restart."""
    try:
        output = await _run_docker([
            "ps", "--filter", "name=omc-session-", "--format",
            "{{.Names}}\t{{.Ports}}\t{{.Status}}",
        ])
        if not output.strip():
            return
        for line in output.strip().split("\n"):
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            name = parts[0]  # omc-session-{slug}
            slug = name.removeprefix("omc-session-")
            ports_str = parts[1]
            status = parts[2] if len(parts) > 2 else ""

            # Parse ports like "127.0.0.1:9100->8080/tcp, 127.0.0.1:9101->8081/tcp, 127.0.0.1:9102->8082/tcp"
            chat_port = nb_port = viz_port = 0
            for mapping in ports_str.split(", "):
                if "->8080" in mapping:
                    chat_port = int(mapping.split(":")[1].split("->")[0])
                elif "->8081" in mapping:
                    nb_port = int(mapping.split(":")[1].split("->")[0])
                elif "->8082" in mapping:
                    viz_port = int(mapping.split(":")[1].split("->")[0])

            if chat_port and nb_port:
                _used_ports.add(chat_port)
                _used_ports.add(nb_port)
                if viz_port:
                    _used_ports.add(viz_port)
                is_running = "Up" in status

                # Re-register the LLM proxy token from the container's env
                session_token = ""
                if is_running:
                    try:
                        env_out = await _run_docker([
                            "exec", name, "printenv", "LLM_API_KEY",
                        ])
                        if env_out.strip():
                            session_token = env_out.strip()
                            from .llm_proxy import _session_tokens
                            _session_tokens[session_token] = {
                                "slug": slug,
                                "user_id": None,
                                "created_at": datetime.utcnow(),
                                "request_count": 0,
                            }
                    except RuntimeError:
                        pass  # container may not be fully up yet

                _sessions[slug] = SessionInfo(
                    slug=slug,
                    container_id=name,
                    chat_port=chat_port,
                    notebook_port=nb_port,
                    viz_port=viz_port,
                    session_token=session_token,
                    status="running" if is_running else "stopped",
                )
                logger.info(f"Recovered session {slug} (chat:{chat_port}, nb:{nb_port}, viz:{viz_port}, "
                            f"token:{'yes' if session_token else 'no'}, {'running' if is_running else 'stopped'})")
    except RuntimeError:
        pass  # docker not available


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
    print(f"[SESSION] _sqsh_mount {slug}: mountpoint={mountpoint}, exists={mountpoint.exists()}", flush=True)

    # Always try to unmount first (clean slate)
    if mountpoint.exists():
        try:
            contents = list(mountpoint.iterdir())
            if contents:
                print(f"[SESSION] _sqsh_mount {slug}: already mounted with {len(contents)} entries", flush=True)
                return mountpoint
        except OSError:
            pass
        # Empty or stale — force unmount and remove directory
        proc = await asyncio.create_subprocess_exec(
            "fusermount", "-u", str(mountpoint),
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        try:
            mountpoint.rmdir()
        except OSError:
            pass

    mountpoint.mkdir(parents=True, exist_ok=True)
    print(f"[SESSION] _sqsh_mount {slug}: running squashfuse...", flush=True)

    # squashfuse is a FUSE daemon — stays running as the filesystem server.
    # Must use subprocess.Popen (not asyncio) because asyncio pipe capture
    # blocks forever on daemon processes that never close their inherited fds.
    # squashfuse is a FUSE daemon — stays running as the filesystem server.
    # Must fully detach: close_fds + start_new_session + DEVNULL streams.
    # asyncio subprocess and capture_output both deadlock because the daemon
    # inherits pipe fds and never closes them.
    import subprocess as _sp
    proc = _sp.Popen(
        ["squashfuse", "-o", "allow_other", str(sqsh_path), str(mountpoint)],
        stdin=_sp.DEVNULL, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
        close_fds=True, start_new_session=True,
    )
    print(f"[SESSION] _sqsh_mount {slug}: squashfuse pid={proc.pid}", flush=True)

    # Wait for the FUSE mount to become visible (daemon needs to initialize)
    for i in range(30):
        await asyncio.sleep(0.5)
        try:
            if list(mountpoint.iterdir()):
                print(f"[SESSION] _sqsh_mount {slug}: ready after {(i+1)*0.5:.1f}s", flush=True)
                return mountpoint
        except OSError:
            pass

    raise RuntimeError(f"squashfuse mount at {mountpoint} not ready after 15s")


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


async def _extract_chat_state(slug: str) -> dict | None:
    """Read chat state from running container via docker exec."""
    try:
        raw = await _run_docker([
            "exec", f"omc-session-{slug}",
            "cat", "/app/.omc/chat_state.json",
        ])
        return json.loads(raw)
    except (RuntimeError, json.JSONDecodeError):
        return None


async def _inject_chat_state(slug: str, state: dict):
    """Write chat state into a container after resume."""
    import base64
    encoded = base64.b64encode(json.dumps(state).encode()).decode()
    await _run_docker([
        "exec", f"omc-session-{slug}",
        "sh", "-c",
        f"mkdir -p /app/.omc && echo '{encoded}' | base64 -d > /app/.omc/chat_state.json",
    ])


async def _commit_chat_to_github(slug: str, state: dict):
    """Commit chat state to paper repo .omc/ directory."""
    import httpx
    import base64
    from .github_app_auth import get_github_headers

    # Look up submission to get github_repo
    async with async_session() as db:
        stmt = select(Submission).where(Submission.slug == slug)
        result = await db.execute(stmt)
        submission = result.scalar_one_or_none()

    if not submission or not submission.github_repo:
        logger.debug(f"No paper repo for {slug}, skipping git commit")
        return

    repo = submission.github_repo  # e.g. "open-community-science/micro-0001"
    api = "https://api.github.com"

    try:
        headers = await get_github_headers()
    except Exception as e:
        logger.warning(f"GitHub auth failed for chat commit: {e}")
        return

    # Prepare transcript (just messages with timestamps)
    transcript = {
        "slug": slug,
        "phase": state.get("phase", "unknown"),
        "message_count": len(state.get("history", [])),
        "saved_at": state.get("saved_at", ""),
        "messages": state.get("history", []),
    }

    # Prepare session metadata (phase, summaries — no messages)
    session_meta = {
        "slug": slug,
        "phase": state.get("phase", "unknown"),
        "message_count": len(state.get("history", [])),
        "interview_summary": state.get("interview_summary", ""),
        "results_summary": state.get("results_summary", ""),
        "saved_at": state.get("saved_at", ""),
    }

    files = {
        ".omc/chat_transcript.json": json.dumps(transcript, indent=2),
        ".omc/session_state.json": json.dumps(session_meta, indent=2),
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        for path, content in files.items():
            content_b64 = base64.b64encode(content.encode()).decode()

            # Check if file exists (need sha for updates)
            get_resp = await client.get(
                f"{api}/repos/{repo}/contents/{path}",
                headers=headers,
            )

            payload = {
                "message": f"Update {path} — {len(state.get('history', []))} messages",
                "content": content_b64,
            }

            if get_resp.status_code == 200:
                payload["sha"] = get_resp.json()["sha"]

            resp = await client.put(
                f"{api}/repos/{repo}/contents/{path}",
                headers=headers,
                json=payload,
            )
            if resp.status_code in (200, 201):
                logger.info(f"Committed {path} to {repo}")
            else:
                logger.warning(f"Failed to commit {path} to {repo}: {resp.status_code} {resp.text[:200]}")


async def launch_session(slug: str, metadata: dict, user_id: int | None = None) -> SessionInfo:
    """Launch a new session container for a submission."""
    if slug in _sessions and _sessions[slug].status == "running":
        # Verify the container is actually alive
        try:
            await _run_docker(["inspect", f"omc-session-{slug}"])
            return _sessions[slug]
        except RuntimeError:
            # Container gone — clean up stale session
            logger.warning(f"Session {slug} was tracked as running but container is gone, relaunching")
            _release_ports(_sessions[slug].chat_port, _sessions[slug].notebook_port, _sessions[slug].viz_port)
            revoke_session_token(slug)
            del _sessions[slug]

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

    chat_port, nb_port, viz_port = _allocate_ports()
    data_path = DATA_BASE_PATH / slug

    # Generate a session-scoped token for the LLM proxy
    session_token = create_session_token(slug, user_id=user_id)

    # Container talks to portal at GATEWAY_IP:PORTAL_PORT
    proxy_base_url = f"http://{GATEWAY_IP}:{PORTAL_PORT}/api/llm"

    # Resolve data source: prefer .sqsh archive, fall back to loose files
    data_mount = ""
    sqsh_mounted = False
    sqsh_path = get_results_path(slug)
    print(f"[SESSION] {slug}: sqsh_path={sqsh_path}, data_path={data_path}, data_path.exists={data_path.exists()}", flush=True)
    import time as _time
    _t0 = _time.time()
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

    # Write metadata.json to host — always available even if no pipeline results yet
    meta_dir = SQSH_MOUNT_BASE / f"{slug}-meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    meta_file = meta_dir / "metadata.json"
    meta_file.write_text(json.dumps(metadata, indent=2, default=str))

    container_name = f"omc-session-{slug}"
    cmd = [
        "run", "-d",
        "--name", container_name,
        "--network", SESSION_NETWORK,
        "-p", f"127.0.0.1:{chat_port}:8080",
        "-p", f"127.0.0.1:{nb_port}:8081",
        "-p", f"127.0.0.1:{viz_port}:8082",
        # LLM goes through the portal proxy — no direct access
        "-e", f"LLM_BASE_URL={proxy_base_url}",
        "-e", f"LLM_API_KEY={session_token}",
        "-e", f"LLM_MODEL={settings.llm_model}",
        "-e", f"CHAINLIT_AUTH_SECRET={settings.secret_key}",
        "-e", f"MARIMO_URL=http://localhost:{nb_port}",
        "-e", f"CHAT_ROOT_PATH=/session-proxy/{chat_port}",
        "-e", f"NB_ROOT_PATH=/session-proxy/{nb_port}",
        "-e", f"VIZ_ROOT_PATH=/session-proxy/{viz_port}",
        "--memory", "2g",
        "--cpus", "1.0",
    ]

    # Mount data (squashfuse mountpoint or loose directory) read-only
    if data_mount:
        cmd.extend(["-v", f"{data_mount}:/data:ro"])

    # Mount metadata.json separately (can't overlay on read-only squashfuse /data)
    cmd.extend(["-v", f"{meta_file}:/metadata/metadata.json:ro"])

    # Mount chat_app.py and notebooks from host for live editing (no rebuild needed)
    session_src = Path(__file__).parent.parent.parent / "session"
    if session_src.exists():
        cmd.extend(["-v", f"{session_src / 'chat_app.py'}:/app/chat_app.py:ro"])
        cmd.extend(["-v", f"{session_src / 'tools.py'}:/app/tools.py:ro"])
        cmd.extend(["-v", f"{session_src / 'data_layer.py'}:/app/data_layer.py:ro"])
        cmd.extend(["-v", f"{session_src / 'notebooks'}:/app/notebooks"])
        cmd.extend(["-v", f"{session_src / 'viz_server.py'}:/app/viz_server.py:ro"])
        cmd.extend(["-v", f"{session_src / 'entrypoint.sh'}:/entrypoint.sh:ro"])

    cmd.append(SESSION_IMAGE)

    print(f"[SESSION] {slug}: sqsh_mount took {_time.time()-_t0:.2f}s, data_mount={data_mount}, launching docker...", flush=True)
    _t1 = _time.time()
    try:
        container_id = await _run_docker(cmd)
    except RuntimeError as e:
        print(f"[SESSION] {slug}: docker run FAILED after {_time.time()-_t1:.2f}s: {e}", flush=True)
        _release_ports(chat_port, nb_port, viz_port)
        revoke_session_token(slug)
        raise HTTPException(500, f"Failed to launch session: {e}")
    print(f"[SESSION] {slug}: docker run took {_time.time()-_t1:.2f}s, container={container_id[:12]}", flush=True)

    session = SessionInfo(
        slug=slug,
        container_id=container_id[:12],
        chat_port=chat_port,
        notebook_port=nb_port,
        viz_port=viz_port,
        session_token=session_token,
        data_mount=data_mount,
        sqsh_mounted=sqsh_mounted,
    )
    _sessions[slug] = session
    logger.info(f"Launched session {slug} → container {container_id[:12]} "
                f"(chat:{chat_port}, nb:{nb_port}, viz:{viz_port}, network:{SESSION_NETWORK})")
    return session


async def stop_session(slug: str):
    """Stop (but don't remove) a session container. Extracts chat state first."""
    if slug not in _sessions:
        return
    session = _sessions[slug]

    # Extract chat state before stopping
    if session.status == "running":
        state = await _extract_chat_state(slug)
        if state:
            session.chat_state = state
            # Commit to GitHub in background (don't block stop)
            try:
                await _commit_chat_to_github(slug, state)
            except Exception as e:
                logger.warning(f"Failed to commit chat for {slug}: {e}")

    try:
        await _run_docker(["stop", f"omc-session-{slug}"])
        session.status = "stopped"
        logger.info(f"Stopped session {slug}")
    except RuntimeError:
        pass


async def resume_session(slug: str) -> SessionInfo:
    """Resume a stopped session container, restoring chat state."""
    if slug not in _sessions:
        raise HTTPException(404, "No session found")
    session = _sessions[slug]
    try:
        await _run_docker(["start", f"omc-session-{slug}"])
        session.status = "running"
        logger.info(f"Resumed session {slug}")
    except RuntimeError as e:
        raise HTTPException(500, f"Failed to resume session: {e}")

    # Inject cached chat state if the container-local file was lost
    if session.chat_state:
        try:
            # Wait for container processes to start
            await asyncio.sleep(3)
            await _inject_chat_state(slug, session.chat_state)
            logger.info(f"Injected chat state into {slug}")
        except Exception as e:
            logger.warning(f"Failed to inject chat state for {slug}: {e}")

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
    _release_ports(session.chat_port, session.notebook_port, session.viz_port)
    revoke_session_token(slug)
    del _sessions[slug]
    logger.info(f"Removed session {slug}")


# ── Routes ───────────────────────────────────────────────────────────────────

@router.post("/ena/launch")
async def launch_ena(
    request: Request,
    user: User = Depends(require_user),
):
    """Launch an ENA metadata session."""
    session = await launch_ena_session(user.id)
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        return RedirectResponse(f"/sessions/{session.slug}/chat", status_code=303)
    return {
        "session_key": session.slug,
        "status": session.status,
        "chat_url": f"/sessions/{session.slug}/chat",
        "notebook_url": f"/sessions/{session.slug}/notebook",
    }


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

    # Full metadata — written to host as metadata.json and mounted into container
    metadata = {
        "slug": submission.slug,
        "accession": submission.bioproject_accession,
        "pipeline": submission.pipeline.value,
        "title": submission.title or "",
        "sample_metadata": submission.sample_metadata or {},
        "interview_data": submission.interview_data or {},
        "selected_runs": submission.selected_runs or [],
    }

    session = await launch_session(slug, metadata, user_id=user.id)

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
    is_ena = slug.startswith("ena-")
    return templates.TemplateResponse(
        "session.html",
        {
            "request": request,
            "user": user,
            "slug": slug,
            "chat_port": session.chat_port,
            "notebook_port": session.notebook_port,
            "viz_port": session.viz_port,
            "is_ena": is_ena,
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


@router.post("/{slug}/save")
async def save(
    slug: str,
    user: User = Depends(require_user),
):
    """Save chat state to GitHub without stopping the session."""
    if slug not in _sessions or _sessions[slug].status != "running":
        raise HTTPException(404, "Session not running")
    state = await _extract_chat_state(slug)
    if not state:
        raise HTTPException(404, "No chat state found")
    _sessions[slug].chat_state = state
    await _commit_chat_to_github(slug, state)
    return {"slug": slug, "status": "saved"}


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
            "viz_port": s.viz_port,
            "started_at": s.started_at.isoformat(),
        }
        for slug, s in _sessions.items()
    }


async def launch_ena_session(user_id: int) -> SessionInfo:
    """Launch an ENA metadata session (not tied to a submission)."""
    import hashlib
    import time

    short_id = hashlib.sha256(f"{user_id}-{time.time()}".encode()).hexdigest()[:8]
    session_key = f"ena-{user_id}-{short_id}"

    if session_key in _sessions and _sessions[session_key].status == "running":
        try:
            await _run_docker(["inspect", f"omc-ena-{user_id}-{short_id}"])
            return _sessions[session_key]
        except RuntimeError:
            _release_ports(_sessions[session_key].chat_port, _sessions[session_key].notebook_port, _sessions[session_key].viz_port)
            revoke_session_token(session_key)
            del _sessions[session_key]

    await _ensure_network()

    container_name = f"omc-ena-{user_id}-{short_id}"

    chat_port, nb_port, viz_port = _allocate_ports()
    session_token = create_session_token(session_key, user_id=user_id)
    proxy_base_url = f"http://{GATEWAY_IP}:{PORTAL_PORT}/api/llm"

    # Writable workspace for TSV output
    import tempfile
    workspace_dir = Path(tempfile.mkdtemp(prefix=f"omc-ena-{user_id}-"))

    cmd = [
        "run", "-d",
        "--name", container_name,
        "--network", SESSION_NETWORK,
        "-p", f"127.0.0.1:{chat_port}:8080",
        "-p", f"127.0.0.1:{nb_port}:8081",
        "-p", f"127.0.0.1:{viz_port}:8082",
        "-e", f"LLM_BASE_URL={proxy_base_url}",
        "-e", f"LLM_API_KEY={session_token}",
        "-e", f"LLM_MODEL={settings.llm_model}",
        "-e", f"CHAINLIT_AUTH_SECRET={settings.secret_key}",
        "-e", f"CHAT_ROOT_PATH=/session-proxy/{chat_port}",
        "-e", f"NB_ROOT_PATH=/session-proxy/{nb_port}",
        "-e", f"VIZ_ROOT_PATH=/session-proxy/{viz_port}",
        "-e", "SESSION_TYPE=ena",
        "-e", f"WORKSPACE_DIR=/workspace",
        "-v", f"{workspace_dir}:/workspace",
        "--memory", "2g",
        "--cpus", "1.0",
    ]

    # Mount session source files for live editing
    session_src = Path(__file__).parent.parent.parent / "session"
    if session_src.exists():
        cmd.extend(["-v", f"{session_src / 'chat_app.py'}:/app/chat_app.py:ro"])
        cmd.extend(["-v", f"{session_src / 'tools.py'}:/app/tools.py:ro"])
        cmd.extend(["-v", f"{session_src / 'data_layer.py'}:/app/data_layer.py:ro"])
        cmd.extend(["-v", f"{session_src / 'notebooks'}:/app/notebooks"])
        cmd.extend(["-v", f"{session_src / 'entrypoint.sh'}:/entrypoint.sh:ro"])

    cmd.append(SESSION_IMAGE)

    try:
        container_id = await _run_docker(cmd)
    except RuntimeError as e:
        _release_ports(chat_port, nb_port, viz_port)
        revoke_session_token(session_key)
        raise HTTPException(500, f"Failed to launch ENA session: {e}")

    session = SessionInfo(
        slug=session_key,
        container_id=container_id[:12],
        chat_port=chat_port,
        notebook_port=nb_port,
        viz_port=viz_port,
        session_token=session_token,
        data_mount=str(workspace_dir),
    )
    _sessions[session_key] = session
    logger.info(f"Launched ENA session {session_key} → {container_id[:12]}")
    return session


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
        "viz_port": session.viz_port,
        "chat_url": f"/sessions/{slug}/chat",
        "chat_proxy": f"/session-proxy/{session.chat_port}/",
        "notebook_proxy": f"/session-proxy/{session.notebook_port}/",
        "viz_proxy": f"/session-proxy/{session.viz_port}/",
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
