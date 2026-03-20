"""OMC Portal - Main FastAPI application."""
import sys
from pathlib import Path as _Path

# Add project root to path so 'ai' package is importable
_project_root = str(_Path(__file__).parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from fastapi import FastAPI, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from contextlib import asynccontextmanager
from pathlib import Path
import asyncio
import logging

from .config import get_settings
from .database import init_db, get_db, async_session, Submission, User
from .auth import router as auth_router, get_current_user
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

    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "user": user, "submissions": submissions},
    )


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

    return templates.TemplateResponse(
        "submission_detail.html",
        {"request": request, "user": user, "submission": submission},
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
