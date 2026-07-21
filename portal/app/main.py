"""OMC Portal - Main FastAPI application."""
import sys
from pathlib import Path as _Path

# Add project root to path so 'ai' package is importable
_project_root = str(_Path(__file__).parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from fastapi import FastAPI, Request, Depends, Form as FForm, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from contextlib import asynccontextmanager
from pathlib import Path
import asyncio
import logging

from .config import get_settings
from .database import init_db, get_db, async_session, Submission, User, SubmissionStatus, PipelineType
from .auth import router as auth_router, get_current_user, is_admin, require_admin
from .submissions import router as submissions_router
from .interviews import router as interviews_router
from .reviews import router as reviews_router
from .metadata import router as metadata_router
from .staging import router as staging_router
from .sessions import router as sessions_router
from .llm_proxy import router as llm_proxy_router
from .openrouter import router as openrouter_router
from .ena import router as ena_router

settings = get_settings()
logger = logging.getLogger(__name__)

# Setup paths
BASE_DIR = Path(__file__).parent.parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")
# Expose is_admin() to all templates (e.g. to gate the Admin nav link)
templates.env.globals["is_admin"] = is_admin


async def _poll_hpc_jobs():
    """Background task: poll HPC for job status updates every 60s."""
    from .slurm import poll_all_running_jobs

    while True:
        await asyncio.sleep(60)
        try:
            async with async_session() as db:
                completed = await poll_all_running_jobs(db)
                if completed:
                    logger.info(f"Background poll: {len(completed)} job(s) finished: {completed}")
        except Exception as e:
            logger.warning(f"Background poll error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database on startup, run background poller, recover sessions."""
    await init_db()
    # Recover any session containers still running from before restart
    from .sessions import _recover_sessions
    await _recover_sessions()
    poll_task = None
    if settings.slurm_enabled:
        poll_task = asyncio.create_task(_poll_hpc_jobs())
        logger.info("Started HPC job poller (60s interval)")
    yield
    if poll_task:
        poll_task.cancel()


app = FastAPI(
    title="OMC Portal",
    description="Open Microbial Community - AI-assisted scientific publishing",
    version="0.1.0",
    lifespan=lifespan,
)

# Add session middleware for auth
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)

# Mount static files
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# Include routers
app.include_router(auth_router)
app.include_router(submissions_router)
app.include_router(interviews_router)
app.include_router(reviews_router)
app.include_router(metadata_router)
app.include_router(staging_router)
app.include_router(sessions_router)
app.include_router(llm_proxy_router)
app.include_router(openrouter_router)
app.include_router(ena_router)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request, db: AsyncSession = Depends(get_db)):
    """Landing page."""
    user = await get_current_user(request, db)
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "user": user},
    )


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    """User dashboard with submissions."""
    user = await get_current_user(request, db)
    if not user:
        return templates.TemplateResponse(
            "login_required.html",
            {"request": request},
        )

    # Get user's submissions
    stmt = select(Submission).where(Submission.user_id == user.id, Submission.deleted_at.is_(None)).order_by(Submission.created_at.desc())
    result = await db.execute(stmt)
    submissions = result.scalars().all()

    # Find active ENA sessions for this user
    from .sessions import _sessions
    ena_sessions = {
        key: {
            "status": s.status,
            "started_at": s.started_at.strftime("%Y-%m-%d %H:%M"),
            "chat_port": s.chat_port,
            "display_name": s.display_name,
        }
        for key, s in _sessions.items()
        if key.startswith(f"ena-{user.id}-")
    }

    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "user": user, "submissions": submissions, "ena_sessions": ena_sessions},
    )


# Stages in pipeline order, and how they roll up for the summary cards.
_STAGE_ORDER = [
    SubmissionStatus.DRAFT, SubmissionStatus.SUBMITTED, SubmissionStatus.QUEUED,
    SubmissionStatus.RUNNING, SubmissionStatus.PROCESSING, SubmissionStatus.DRAFTING,
    SubmissionStatus.REVIEW, SubmissionStatus.PUBLISHED, SubmissionStatus.FAILED,
]
_ACTIVE_STATES = {
    SubmissionStatus.SUBMITTED, SubmissionStatus.QUEUED, SubmissionStatus.RUNNING,
    SubmissionStatus.PROCESSING, SubmissionStatus.DRAFTING, SubmissionStatus.REVIEW,
}
# Active jobs older than this (hours) are flagged as possibly stuck.
_STUCK_HOURS = 24.0


def _hours_between(a, b):
    """Whole-hours float between two datetimes, or None if either is missing."""
    if not a or not b:
        return None
    return (b - a).total_seconds() / 3600.0


def _pctl(values, p):
    """Simple nearest-rank percentile on a list of floats (p in 0..100)."""
    if not values:
        return None
    s = sorted(values)
    if p <= 0:
        return s[0]
    if p >= 100:
        return s[-1]
    import math
    k = max(0, math.ceil(p / 100.0 * len(s)) - 1)
    return s[k]


async def _check_local_llm() -> dict:
    """Ping the configured local/base LLM (settings.llm_base_url) for reachability."""
    import httpx
    url = settings.llm_base_url.rstrip("/") + "/models"
    try:
        async with httpx.AsyncClient(timeout=6.0) as c:
            r = await c.get(url, headers={"Authorization": f"Bearer {settings.llm_api_key}"})
        if r.status_code == 200:
            try:
                n = len(r.json().get("data", []))
            except Exception:
                n = None
            return {"ok": True, "detail": f"{n} model(s)" if n is not None else "reachable"}
        return {"ok": False, "detail": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"ok": False, "detail": type(e).__name__}


async def _check_openrouter_key(key: str) -> dict:
    """Validate an OpenRouter key via /auth/key. Returns status + limit info."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get("https://openrouter.ai/api/v1/auth/key",
                            headers={"Authorization": f"Bearer {key}"})
        if r.status_code == 200:
            d = r.json().get("data", {})
            lim = d.get("limit")
            used = d.get("usage")
            detail = "free/unlimited" if lim is None else f"${used or 0:.2f} / ${lim}"
            return {"ok": True, "detail": detail}
        try:
            msg = r.json().get("error", {}).get("message", "")
        except Exception:
            msg = ""
        return {"ok": False, "detail": f"HTTP {r.status_code}: {msg}"[:80]}
    except Exception as e:
        return {"ok": False, "detail": type(e).__name__}


@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request, db: AsyncSession = Depends(get_db)):
    """Site-owner admin panel: users, per-stage job matrix, operational stats.

    Access is gated to configured admins (ADMIN_GITHUB_LOGINS); dev mode grants
    the dev user. All figures are derived from the portal DB plus the HPC status
    JSON pushed by fir — no SSH to the cluster.
    """
    from datetime import datetime
    from .staging import get_hpc_status, get_cluster_status

    user = await get_current_user(request, db)
    if not is_admin(user):
        # Distinguish "log in" from "not allowed" for a nicer UX.
        if not user:
            return templates.TemplateResponse("login_required.html", {"request": request})
        return templates.TemplateResponse(
            "404.html", {"request": request, "user": user}, status_code=403,
        )

    now = datetime.utcnow()

    # Pull all users and their (non-deleted) submissions.
    users = (await db.execute(select(User))).scalars().all()
    subs = (await db.execute(
        select(Submission).where(Submission.deleted_at.is_(None))
    )).scalars().all()

    users_by_id = {u.id: u for u in users}

    # ── Per-user rollup ─────────────────────────────────────────────
    def _blank_stage_counts():
        return {s.value: 0 for s in _STAGE_ORDER}

    per_user = {}
    for u in users:
        per_user[u.id] = {
            "login": u.github_login,
            "name": u.github_name or "",
            "avatar": u.github_avatar_url or "",
            "last_login": u.last_login,
            "total": 0,
            "active": 0,
            "published": 0,
            "failed": 0,
            "stages": _blank_stage_counts(),
            "pipelines": {},  # pipeline value -> count
        }

    # ── Pipeline × stage matrix (global) ────────────────────────────
    matrix = {}  # pipeline value -> {stage value -> count}
    pipelines_present = set()

    # ── Duration samples ────────────────────────────────────────────
    turnaround_h = []   # published: submitted -> completed
    fail_time_h = []    # failed: submitted -> completed
    active_rows = []    # live jobs with age
    failure_rows = []   # recent failures with detail

    for s in subs:
        status = s.status if isinstance(s.status, SubmissionStatus) else SubmissionStatus(s.status)
        pipe = (s.pipeline.value if s.pipeline else "unassigned")
        pipelines_present.add(pipe)
        stage_v = status.value

        bucket = per_user.get(s.user_id)
        if bucket is not None:
            bucket["total"] += 1
            bucket["stages"][stage_v] = bucket["stages"].get(stage_v, 0) + 1
            bucket["pipelines"][pipe] = bucket["pipelines"].get(pipe, 0) + 1
            if status in _ACTIVE_STATES:
                bucket["active"] += 1
            elif status == SubmissionStatus.PUBLISHED:
                bucket["published"] += 1
            elif status == SubmissionStatus.FAILED:
                bucket["failed"] += 1

        matrix.setdefault(pipe, _blank_stage_counts())
        matrix[pipe][stage_v] = matrix[pipe].get(stage_v, 0) + 1

        # Durations
        if status == SubmissionStatus.PUBLISHED:
            d = _hours_between(s.submitted_at or s.created_at, s.completed_at)
            if d is not None and d >= 0:
                turnaround_h.append(d)
        elif status == SubmissionStatus.FAILED:
            d = _hours_between(s.submitted_at or s.created_at, s.completed_at)
            if d is not None and d >= 0:
                fail_time_h.append(d)

        # Live jobs + issues need the pushed HPC status.
        if status in _ACTIVE_STATES or status == SubmissionStatus.FAILED:
            hpc = get_hpc_status(s.slug) or {}
            age = _hours_between(s.submitted_at or s.created_at, now)
            owner = users_by_id.get(s.user_id)
            row = {
                "slug": s.slug,
                "login": owner.github_login if owner else "?",
                "pipeline": pipe,
                "status": stage_v,
                "age_h": age,
                "hpc_phase": hpc.get("phase", ""),
                "detail": hpc.get("detail", "") or hpc.get("slurm_state", ""),
            }
            if status in _ACTIVE_STATES:
                row["stuck"] = age is not None and age > _STUCK_HOURS
                active_rows.append(row)
            else:  # FAILED
                row["reason"] = hpc.get("reason", "")
                row["exit_code"] = hpc.get("exit_code", "")
                row["error"] = (s.error_message or "")[:400]
                row["when"] = s.completed_at or s.submitted_at or s.created_at
                failure_rows.append(row)

    # Order the matrix rows/columns and compute totals.
    stage_values = [s.value for s in _STAGE_ORDER]
    matrix_rows = []
    col_totals = {sv: 0 for sv in stage_values}
    grand = 0
    for pipe in sorted(pipelines_present):
        counts = matrix.get(pipe, {})
        row_total = 0
        cells = []
        for sv in stage_values:
            n = counts.get(sv, 0)
            cells.append(n)
            col_totals[sv] += n
            row_total += n
        grand += row_total
        matrix_rows.append({"pipeline": pipe, "cells": cells, "total": row_total})

    # Summary + issues.
    active_total = sum(u["active"] for u in per_user.values())
    published_total = sum(u["published"] for u in per_user.values())
    failed_total = sum(u["failed"] for u in per_user.values())
    terminal = published_total + failed_total

    active_rows.sort(key=lambda r: (r.get("age_h") or 0), reverse=True)
    failure_rows.sort(key=lambda r: (r["when"] or now), reverse=True)
    stuck_rows = [r for r in active_rows if r.get("stuck")]

    stats = {
        "n_users": len(users),
        "total": len(subs),
        "active": active_total,
        "published": published_total,
        "failed": failed_total,
        "failure_rate": (100.0 * failed_total / terminal) if terminal else None,
        "turnaround_median_h": _pctl(turnaround_h, 50),
        "turnaround_p90_h": _pctl(turnaround_h, 90),
        "turnaround_max_h": _pctl(turnaround_h, 100),
        "turnaround_n": len(turnaround_h),
        "fail_time_median_h": _pctl(fail_time_h, 50),
        "fail_time_n": len(fail_time_h),
        "stuck": len(stuck_rows),
    }

    # Users sorted by total desc, then login.
    user_list = sorted(
        per_user.values(), key=lambda u: (-u["total"], (u["login"] or "").lower())
    )

    # HPC clusters: heartbeat status + which one is active (failover switch).
    cluster_info = get_cluster_status()

    # LLM / API-key health: local LLM reachability + each user's OpenRouter key.
    from .crypto import decrypt_value
    llm_local = await _check_local_llm()
    or_rows = []
    for u in users:
        row = {"user_id": u.id, "login": u.github_login,
               "model": u.openrouter_model or "", "masked": "", "status": None}
        if u.openrouter_key:
            try:
                key = decrypt_value(u.openrouter_key)
                row["masked"] = (key[:10] + "…" + key[-4:]) if key else ""
                row["status"] = await _check_openrouter_key(key)
            except Exception:
                row["status"] = {"ok": False, "detail": "decrypt failed"}
        or_rows.append(row)

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "user": user,
            "stats": stats,
            "users": user_list,
            "stage_values": stage_values,
            "matrix_rows": matrix_rows,
            "col_totals": [col_totals[sv] for sv in stage_values],
            "grand_total": grand,
            "active_rows": active_rows,
            "failure_rows": failure_rows,
            "stuck_hours": _STUCK_HOURS,
            "generated_at": now.strftime("%Y-%m-%d %H:%M UTC"),
            "clusters": cluster_info["clusters"],
            "active_cluster": cluster_info["active"],
            "llm_local": llm_local,
            "llm_base_url": settings.llm_base_url,
            "llm_model": settings.llm_model,
            "or_rows": or_rows,
        },
    )


@app.post("/admin/llm/openrouter")
async def admin_set_openrouter(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user_id: int = FForm(...),
    key: str = FForm(""),
    model: str = FForm(""),
):
    """Admin: rotate or clear a user's OpenRouter key (and optionally model)."""
    from .crypto import encrypt_value
    from sqlalchemy.orm import attributes as _attrs
    admin = await get_current_user(request, db)
    if not is_admin(admin):
        raise HTTPException(status_code=403, detail="Admin access required")
    target = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    key = key.strip()
    if key:
        if not key.startswith("sk-or-"):
            raise HTTPException(status_code=400, detail="OpenRouter keys start with 'sk-or-'")
        target.openrouter_key = encrypt_value(key)
    else:
        target.openrouter_key = None
    _attrs.flag_modified(target, "openrouter_key")
    if model.strip():
        target.openrouter_model = model.strip()
    await db.commit()

    # Invalidate the proxy's cached config so the new key takes effect immediately.
    try:
        from .llm_proxy import _openrouter_cache
        _openrouter_cache.pop(user_id, None)
    except Exception:
        pass
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/cluster/active")
async def admin_set_active_cluster(
    request: Request,
    db: AsyncSession = Depends(get_db),
    cluster: str = FForm(...),
):
    """Admin: designate the active HPC cluster (pickup failover switch).

    The clusters' loops read this on their next heartbeat and start/stop picking
    up new jobs accordingly — no SSH needed. In-flight jobs keep reconciling on
    whichever cluster is running them.
    """
    admin = await get_current_user(request, db)
    if not is_admin(admin):
        raise HTTPException(status_code=403, detail="Admin access required")
    name = cluster.strip()
    if not name:
        raise HTTPException(status_code=400, detail="cluster name required")
    from .staging import set_active_cluster
    set_active_cluster(name)
    return RedirectResponse(url="/admin", status_code=303)


@app.get("/submissions/{slug}", response_class=HTMLResponse)
async def submission_detail(
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Submission detail page."""
    user = await get_current_user(request, db)
    if not user:
        return templates.TemplateResponse(
            "login_required.html",
            {"request": request},
        )

    stmt = select(Submission).where(
        Submission.slug == slug,
        Submission.user_id == user.id,
        Submission.deleted_at.is_(None),
    )
    result = await db.execute(stmt)
    submission = result.scalar_one_or_none()

    if not submission:
        return templates.TemplateResponse(
            "404.html",
            {"request": request, "user": user},
            status_code=404,
        )

    # Try to load pipeline versions.yml from results
    pipeline_versions = None
    for base in [Path("/mnt/omc-sessions") / slug, Path(settings.local_download_path) / slug]:
        versions_path = base / "pipeline_info" / "versions.yml"
        if versions_path.exists():
            try:
                import yaml
                pipeline_versions = yaml.safe_load(versions_path.read_text())
            except Exception:
                pass
            break

    return templates.TemplateResponse(
        "submission_detail.html",
        {"request": request, "user": user, "submission": submission, "pipeline_versions": pipeline_versions},
    )


@app.get("/submissions/{slug}/interview", response_class=HTMLResponse)
async def interview_page(
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Author interview page."""
    user = await get_current_user(request, db)
    if not user:
        return templates.TemplateResponse(
            "login_required.html",
            {"request": request},
        )

    stmt = select(Submission).where(
        Submission.slug == slug,
        Submission.user_id == user.id,
        Submission.deleted_at.is_(None),
    )
    result = await db.execute(stmt)
    submission = result.scalar_one_or_none()

    if not submission:
        return templates.TemplateResponse(
            "404.html",
            {"request": request, "user": user},
            status_code=404,
        )

    return templates.TemplateResponse(
        "interview.html",
        {"request": request, "user": user, "submission": submission},
    )


@app.get("/submissions/{slug}/manuscript", response_class=HTMLResponse)
async def manuscript_preview(
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Manuscript preview page — view and edit before publishing to GitHub."""
    user = await get_current_user(request, db)
    if not user:
        return templates.TemplateResponse("login_required.html", {"request": request})

    stmt = select(Submission).where(
        Submission.slug == slug,
        Submission.user_id == user.id,
        Submission.deleted_at.is_(None),
    )
    result = await db.execute(stmt)
    submission = result.scalar_one_or_none()
    if not submission:
        return templates.TemplateResponse("404.html", {"request": request, "user": user}, status_code=404)

    interview_data = submission.interview_data or {}
    manuscript = interview_data.get("_manuscript")
    if not manuscript:
        return RedirectResponse(f"/submissions/{slug}", status_code=303)

    # Render markdown to HTML for display
    import markdown
    manuscript_html = {}
    for name, text in manuscript.items():
        if isinstance(text, str):
            manuscript_html[name] = markdown.markdown(
                text, extensions=["tables", "fenced_code"],
            )
        else:
            manuscript_html[name] = text

    return templates.TemplateResponse(
        "manuscript_preview.html",
        {
            "request": request,
            "user": user,
            "submission": submission,
            "manuscript": manuscript,
            "manuscript_html": manuscript_html,
        },
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
