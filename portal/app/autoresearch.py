"""Claim-grounded autoresearch endpoints (issue #29).

Drives the reusable ``ai.autoresearch`` core from the website. An author triggers
an autonomous analysis run over their submission's pipeline data; the agent
proposes an agenda, runs its own analysis code, records verifiable claims, then a
two-layer verification (deterministic re-execution + skeptical reconciliation)
grades each claim before the Results prose is written from the verified ones.

This mirrors ``reviews.py`` (background work, SSE progress, ``resolve_llm`` for the
backend, persistence to ``Submission.interview_data``, optional ``.omc/`` commit),
but respects the HARD CONSTRAINTS of #29:

  * the agent's tool-calling loop runs server-side here (it orchestrates only — it
    never runs model code on the portal host);
  * model-written ``run_analysis`` code runs inside the isolated ``omc-session``
    container via ``ContainerExecutor`` (``docker exec`` into the sandbox);
  * the deterministic re-read half of ``verify()`` uses a ``DirDataSource`` over the
    SAME squashfuse ``.sqsh`` mount the container reads, preserving determinism;
  * the LLM is acquired only through ``resolve_llm`` (no hardcoded endpoint).

Routes:
  * ``POST /autoresearch/{slug}/run-stream`` — SSE run (``?resume=true`` to keep
    digging from the prior snapshot) + persistence + optional PR.
  * ``GET  /autoresearch/{slug}/findings`` — the claim/evidence findings viewer.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import attributes
from sqlalchemy import select

from .config import get_settings
from .database import get_db, async_session, Submission, User
from .auth import require_user, get_current_user
from .templating import templates

router = APIRouter(prefix="/autoresearch", tags=["autoresearch"])
settings = get_settings()
logger = logging.getLogger(__name__)


# ── helpers (mirrors reviews.py) ──────────────────────────────────────────────
async def _get_llm_config(user_id: int) -> dict:
    """Resolve the LLM config for a user's chosen backend (local / shared-admin /
    personal OpenRouter), honouring the explicit /settings choice via
    ``llm_backends.resolve_llm``. Copied from ``reviews.py`` so the two generation
    surfaces route the LLM identically."""
    from sqlalchemy import select as _select
    from .database import User as _User
    from .llm_backends import resolve_llm
    from .database import async_session as _session
    async with _session() as _db:
        target = (await _db.execute(_select(_User).where(_User.id == user_id))).scalar_one_or_none()
    return await resolve_llm(target)


def _dir_has_content(path) -> bool:
    """True when ``path`` exists and is a non-empty directory (not just an empty
    mountpoint). Copied from ``reviews.py``."""
    p = Path(path)
    if not p.exists():
        return False
    try:
        return any(p.iterdir())
    except OSError:
        return False


async def ensure_session_running(slug: str, metadata: dict, user_id: int | None):
    """Guarantee the ``omc-session-{slug}`` container is up before the executor is
    built — the HARD CONSTRAINT that model code runs in the sandbox, never on the host.

    Delegates to ``sessions.launch_session``, which is idempotent: it returns the
    live container if one is already running, resumes a stopped one, and relaunches
    a dead/orphaned one. A short readiness wait mirrors ``resume_session``'s ~3s
    grace for the container's processes (and the ``/data`` mount) to come up before
    the first ``docker exec``. Raises on failure so the caller can emit a clean
    ``error`` event."""
    from .sessions import launch_session
    session = await launch_session(slug, metadata, user_id=user_id)
    await asyncio.sleep(3)  # let container processes + /data mount settle
    return session


async def _build_data_source(slug: str, study: dict):
    """Build a ``DirDataSource`` over the submission's viz JSON — the server-side
    reads for the agenda tools AND the deterministic re-read half of ``verify()``.

    Points at the host squashfuse mount of ``{slug}.sqsh`` (the SAME ``.sqsh``
    bind-mounted read-only into the container at ``/data``), so server-side
    ``navigate`` and in-container re-execution see identical bytes. Mirrors the
    mount/fallback handling in ``reviews.py`` (best-effort mount, ``_dir_has_content``
    guard, fall back to ``local_download_path/{slug}``)."""
    from .staging import get_results_path
    from .sessions import _sqsh_mount, SQSH_MOUNT_BASE
    from ai.autoresearch import DirDataSource

    sqsh_mount = SQSH_MOUNT_BASE / slug
    sqsh_path = get_results_path(slug)
    if sqsh_path and not _dir_has_content(sqsh_mount):
        try:
            sqsh_mount = await _sqsh_mount(slug, sqsh_path)
        except Exception as e:
            logger.warning(f"Could not mount sqsh for {slug}: {e}")

    viz_dir = None
    for base in (sqsh_mount, Path(settings.local_download_path) / slug):
        cand = Path(base) / "viz"
        if _dir_has_content(cand):
            viz_dir = cand
            break
    if viz_dir is None:
        raise RuntimeError(
            "No pipeline viz data found (expected .../viz) — autoresearch "
            "requires completed pipeline results (.sqsh) for this submission.")
    return DirDataSource(viz_dir, study=study)


# ── run (SSE) ─────────────────────────────────────────────────────────────────
@router.post("/{slug}/run-stream")
async def run_autoresearch_stream(
    slug: str,
    resume: bool = False,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Run claim-grounded autoresearch with SSE progress streaming.

    Clone of ``reviews.generate_manuscript_stream``: snapshot the submission fields
    to locals, resolve the LLM, ensure the session container is running, mount the
    data, then drive ``Autoresearcher.explore()`` → ``verify()`` → ``write_results()``
    on a background task while forwarding tool-call / verification events over SSE.
    On completion the resolved snapshot is persisted to
    ``interview_data['_autoresearch']`` in a fresh DB session and (optionally) the
    Results prose is committed to the paper repo's ``.omc/`` via a PR.

    With ``resume=true`` ("keep digging") the prior persisted snapshot is
    reconstructed and the agent CONTINUES from where it stopped — running another
    ``max_steps`` batch that appends to the same ledger. The user stops simply by
    not asking for another batch."""
    if not settings.autoresearch_enabled:
        raise HTTPException(status_code=403, detail="Autoresearch is not enabled.")

    stmt = select(Submission).where(
        Submission.slug == slug,
        Submission.user_id == user.id,
    )
    result = await db.execute(stmt)
    submission = result.scalar_one_or_none()
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")

    # Snapshot submission fields to locals before we leave the db-session scope.
    sub_slug = submission.slug
    sub_id = submission.id
    sub_accession = submission.bioproject_accession
    sub_pipeline = submission.pipeline.value
    sub_title = submission.title or ""
    sub_study_meta = dict(submission.sample_metadata or {})
    sub_interview = dict(submission.interview_data or {})
    sub_github_repo = submission.github_repo
    sub_selected_runs = submission.selected_runs or []

    # Metadata written to the container at launch (same shape as sessions.launch).
    session_metadata = {
        "slug": sub_slug,
        "accession": sub_accession,
        "pipeline": sub_pipeline,
        "title": sub_title,
        "sample_metadata": sub_study_meta,
        "interview_data": sub_interview,
        "selected_runs": sub_selected_runs,
    }

    llm = await _get_llm_config(user.id)
    user_id = user.id
    # Per-user depth (blank → site default). See /settings/autoresearch.
    max_steps = user.autoresearch_max_steps or settings.autoresearch_max_steps

    # "Keep digging": reconstruct from the prior snapshot and continue. Falls back
    # to a fresh run if resume was asked but nothing has been persisted yet.
    prior_snapshot = sub_interview.get("_autoresearch") if resume else None
    resuming = bool(prior_snapshot)

    result_holder: dict = {}

    async def event_stream():
        from openai import AsyncOpenAI
        from ai.autoresearch import Autoresearcher, LLMClient

        progress_queue: asyncio.Queue = asyncio.Queue()
        await progress_queue.put({"event": "start", "detail": f"Using {llm['label']}"})

        async def on_progress(event, detail):
            await progress_queue.put({"event": event, "detail": detail})

        async def run():
            try:
                # 1. Session container must be running — model code runs in the sandbox.
                await progress_queue.put({"event": "session", "detail": "starting session container"})
                try:
                    await ensure_session_running(sub_slug, session_metadata, user_id)
                except Exception as e:
                    await progress_queue.put({"event": "error",
                                              "detail": f"Could not start session container: {e}"})
                    return
                await progress_queue.put({"event": "session", "detail": "container ready"})

                # 2. Data source (server-side re-reads) + container executor (sandbox).
                data = await _build_data_source(sub_slug, sub_study_meta)
                # Pre-warm the cached datasets off the event loop — the first read
                # parses renorm/samples/taxonomy/provenance JSON and would otherwise
                # block every other in-flight request inline (large taxonomy files).
                await asyncio.to_thread(data.datasets)
                from .autoresearch_executor import ContainerExecutor  # Task B (portal-side)
                try:
                    executor = ContainerExecutor(
                        sub_slug, default_timeout=settings.autoresearch_max_analysis_s)
                except TypeError:
                    # Forward-compatible with the design's ContainerExecutor(slug)
                    # signature; per-exec timeout then falls to the executor default.
                    executor = ContainerExecutor(sub_slug)

                # 3. LLM client (server-side; tool-calling passes straight through).
                llm_client = LLMClient(
                    AsyncOpenAI(base_url=llm["base_url"], api_key=llm["api_key"]),
                    model=llm["model"])

                ar_kwargs = dict(
                    explore_model=settings.role_model("explore", llm["model"]),
                    verify_model=settings.role_model("verify", llm["model"]),
                    max_steps=max_steps,
                    max_followups=settings.autoresearch_max_followups,
                    reconcile=settings.autoresearch_reconcile_enabled,
                    on_progress=on_progress)
                if resuming:
                    await progress_queue.put({"event": "session",
                                              "detail": f"resuming from {len(prior_snapshot.get('claims', []))} prior claims"})
                    ar = Autoresearcher.from_snapshot(
                        prior_snapshot, data, llm_client, executor, **ar_kwargs)
                else:
                    ar = Autoresearcher(data, llm_client, executor, **ar_kwargs)

                # 4. Explore (time-budgeted) → verify → write prose → snapshot.
                completed = await asyncio.wait_for(
                    ar.explore(resume=resuming), timeout=settings.autoresearch_time_budget_s)
                await progress_queue.put({"event": "verify",
                                          "detail": "re-executing claims for verification"})
                await ar.verify()
                await progress_queue.put({"event": "write", "detail": "writing Results prose"})
                await ar.write_results()
                result_holder["snapshot"] = ar.snapshot(resolve=True, completed=completed)
            except asyncio.TimeoutError:
                result_holder["error"] = "time budget exceeded"
                await progress_queue.put({"event": "error",
                                          "detail": "Autoresearch exceeded its time budget."})
            except Exception as e:
                result_holder["error"] = str(e)
                logger.exception(f"Autoresearch run failed for {sub_slug}")
                await progress_queue.put({"event": "error", "detail": str(e)})
            finally:
                await progress_queue.put(None)

        asyncio.create_task(run())

        while True:
            msg = await progress_queue.get()
            if msg is None:
                break
            yield f"data: {json.dumps(msg, default=str)}\n\n"

        # Persist the snapshot in a FRESH session (the request db is out of scope).
        if "snapshot" in result_holder:
            snapshot = result_holder["snapshot"]
            yield f"data: {json.dumps({'event': 'start', 'detail': 'Saving autoresearch results...'})}\n\n"
            try:
                interview_data = dict(sub_interview)
                interview_data["_autoresearch"] = snapshot
                # Provenance: name the backend/model that produced this run (#16 idiom).
                interview_data["_autoresearch_model"] = llm.get("label")
                interview_data["_autoresearch_backend"] = llm.get("backend")

                async with async_session() as save_db:
                    save_result = await save_db.execute(
                        select(Submission).where(Submission.id == sub_id))
                    sub = save_result.scalar_one()
                    sub.interview_data = interview_data
                    # flag_modified is mandatory — JSON mutation isn't autodetected.
                    attributes.flag_modified(sub, "interview_data")
                    await save_db.commit()
            except Exception as e:
                yield f"data: {json.dumps({'event': 'error', 'detail': f'Save failed: {e}'})}\n\n"
                return

            # Optional: commit the Results prose to the paper repo's .omc/ via a PR.
            if settings.autoresearch_commit_enabled and sub_github_repo:
                results_md = snapshot.get("results_prose") or ""
                if results_md.strip():
                    try:
                        from .github_integration import create_review_pr
                        # "https://github.com/org/micro-0001" → "org/micro-0001"
                        repo_full_name = "/".join(
                            sub_github_repo.rstrip("/").split("/")[-2:])
                        pr_url = await create_review_pr(
                            repo_full_name, [{"body": results_md}], "autoresearch")
                        yield f"data: {json.dumps({'event': 'commit', 'detail': pr_url})}\n\n"
                    except Exception as e:
                        logger.warning(f"Autoresearch .omc commit failed for {sub_slug}: {e}")
                        yield f"data: {json.dumps({'event': 'commit', 'detail': f'commit skipped: {e}'})}\n\n"

            complete = {
                "event": "complete",
                "detail": "Autoresearch complete — view the findings",
                "ledger_url": f"/autoresearch/{sub_slug}/findings",
            }
            yield f"data: {json.dumps(complete)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ── findings viewer ───────────────────────────────────────────────────────────
@router.get("/{slug}/findings")
async def findings_viewer(
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Render the findings viewer (the claim/evidence DAG) for an autoresearch run.

    Mirrors ``main.manuscript_preview``: owner-scoped load, redirect to the
    submission page when no run exists. The persisted snapshot is already fully
    resolved (``snapshot(resolve=True)``), so ``findings.html`` is pure-render —
    it consumes ``data_json`` and imports nothing from the bench / ``ai``."""
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
        return templates.TemplateResponse(
            "404.html", {"request": request, "user": user}, status_code=404)

    interview_data = submission.interview_data or {}
    snapshot = interview_data.get("_autoresearch")
    if not snapshot:
        return RedirectResponse(f"/submissions/{slug}", status_code=303)

    return templates.TemplateResponse(
        "findings.html",
        {
            "request": request,
            "user": user,
            "submission": submission,
            # Embedded in a <script> block: escape so model-generated content
            # (claim text / run_analysis code) can't break out via </script> or
            # inject markup (stored-XSS guard). Escapes <, >, & and U+2028/U+2029.
            "data_json": (json.dumps(snapshot, default=str)
                          .replace("<", "\\u003c").replace(">", "\\u003e")
                          .replace("&", "\\u0026").replace("\u2028", "\\u2028")
                          .replace("\u2029", "\\u2029")),
            "model_label": interview_data.get("_autoresearch_model"),
        },
    )
