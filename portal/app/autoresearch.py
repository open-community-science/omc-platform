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

from .run_registry import Run as _Run, registry, stream_run as _stream_run, status_payload, sse_response

router = APIRouter(prefix="/autoresearch", tags=["autoresearch"])
settings = get_settings()
logger = logging.getLogger(__name__)

# Detached, disconnect-surviving runs (see run_registry). slug -> Run.
_RUNS = registry("autoresearch")


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
    # Inferred amplicon primers (fwd/rev, region, per-set) — tells the agent the assay
    # design so a 16S/18S domain split reads as an expected dual-amplicon study rather
    # than a mystery to re-derive. Confirmed per-sample primers will arrive via #37.
    if submission.primers:
        sub_study_meta["primers"] = submission.primers
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

    # Already running for this submission? Attach to it (reconnect) rather than
    # starting a second run — this is what makes navigating back resume the view.
    existing = _RUNS.get(sub_slug)
    if existing and not existing.done:
        return sse_response(_stream_run(existing))

    run = _Run()
    _RUNS[sub_slug] = run

    async def worker():
        """Detached: runs to completion and PERSISTS regardless of any listener."""
        from openai import AsyncOpenAI
        from ai.autoresearch import Autoresearcher, LLMClient

        async def on_progress(event, detail):
            run.emit(event, detail)

        snapshot = None
        try:
            run.emit("start", f"Using {llm['label']}")
            # 1. Session container must be running — model code runs in the sandbox.
            run.emit("session", "starting session container")
            try:
                await ensure_session_running(sub_slug, session_metadata, user_id)
            except Exception as e:
                run.emit("error", f"Could not start session container: {e}")
                run.finish("error"); return
            run.emit("session", "container ready")

            # 2. Data source (server-side re-reads) + container executor (sandbox).
            data = await _build_data_source(sub_slug, sub_study_meta)
            # Pre-warm cached datasets off the event loop (large taxonomy JSON).
            await asyncio.to_thread(data.datasets)
            from .autoresearch_executor import ContainerExecutor
            try:
                executor = ContainerExecutor(
                    sub_slug, default_timeout=settings.autoresearch_max_analysis_s)
            except TypeError:
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
                run.emit("session", f"resuming from {len(prior_snapshot.get('claims', []))} prior claims")
                ar = Autoresearcher.from_snapshot(
                    prior_snapshot, data, llm_client, executor, **ar_kwargs)
            else:
                ar = Autoresearcher(data, llm_client, executor, **ar_kwargs)

            # 4. Explore (time-budgeted) → verify → write prose → snapshot.
            completed = await asyncio.wait_for(
                ar.explore(resume=resuming), timeout=settings.autoresearch_time_budget_s)
            run.emit("verify", "re-executing claims for verification")
            await ar.verify()
            run.emit("write", "writing Results prose")
            await ar.write_results()
            snapshot = ar.snapshot(resolve=True, completed=completed)
        except asyncio.TimeoutError:
            run.emit("error", "Autoresearch exceeded its time budget.")
            run.finish("error"); return
        except Exception as e:
            logger.exception(f"Autoresearch run failed for {sub_slug}")
            run.emit("error", str(e))
            run.finish("error"); return

        # Persist in a FRESH session — ALWAYS, even if no client is still connected.
        run.emit("start", "Saving autoresearch results...")
        try:
            interview_data = dict(sub_interview)
            interview_data["_autoresearch"] = snapshot
            # Provenance (#16). Per-artifact attribution lives on each claim/computation
            # (snapshot .by / .reconciled_by); here we keep the LATEST run's label plus
            # the accumulated ROSTER of every backend·model that has contributed — so
            # "keep digging" with a different model never erases who produced earlier claims.
            interview_data["_autoresearch_model"] = llm.get("label")
            interview_data["_autoresearch_backend"] = llm.get("backend")
            roster = list(sub_interview.get("_autoresearch_models") or [])
            if llm.get("label") and llm["label"] not in roster:
                roster.append(llm["label"])
            interview_data["_autoresearch_models"] = roster

            # Per-run log so the author can see how many reps ("dig deeper") have
            # happened and what each added: timestamp, model, and NEW claims that run.
            prior_n = len(prior_snapshot.get("claims", [])) if resuming else 0
            new_total = len(snapshot.get("claims", []))
            runs = list(sub_interview.get("_autoresearch_runs") or [])
            runs.append({
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "model": settings.role_model("explore", llm["model"]),
                "label": llm.get("label"),
                "new_claims": max(0, new_total - prior_n),
                "total_claims": new_total,
                "resumed": resuming,
            })
            interview_data["_autoresearch_runs"] = runs

            async with async_session() as save_db:
                save_result = await save_db.execute(
                    select(Submission).where(Submission.id == sub_id))
                sub = save_result.scalar_one()
                sub.interview_data = interview_data
                attributes.flag_modified(sub, "interview_data")
                await save_db.commit()
        except Exception as e:
            logger.exception(f"Autoresearch save failed for {sub_slug}")
            run.emit("error", f"Save failed: {e}")
            run.finish("error"); return

        # Optional: commit the Results prose to the paper repo's .omc/ via a PR.
        if settings.autoresearch_commit_enabled and sub_github_repo:
            results_md = snapshot.get("results_prose") or ""
            if results_md.strip():
                try:
                    from .github_integration import create_review_pr
                    repo_full_name = "/".join(sub_github_repo.rstrip("/").split("/")[-2:])
                    pr_url = await create_review_pr(
                        repo_full_name, [{"body": results_md}], "autoresearch")
                    run.emit("commit", pr_url)
                except Exception as e:
                    logger.warning(f"Autoresearch .omc commit failed for {sub_slug}: {e}")
                    run.emit("commit", f"commit skipped: {e}")

        url = f"/autoresearch/{sub_slug}/findings"
        run.push({"event": "complete",
                  "detail": "Autoresearch complete — view the findings", "ledger_url": url})
        run.finish("complete", result_url=url)

    asyncio.create_task(worker())
    return sse_response(_stream_run(run))


@router.get("/{slug}/status")
async def run_status(
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Is a run in flight for this submission? Lets the page reconnect to a live
    run (or show its terminal state) after a navigation, instead of losing it."""
    user = await get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return status_payload(_RUNS.get(slug))


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

    # Per-run history (one row per initial run + each "dig deeper"), formatted for
    # display so the author can count reps and see what each model added.
    runs_display = []
    for r in (interview_data.get("_autoresearch_runs") or []):
        at = r.get("at") or ""
        try:
            at_disp = datetime.fromisoformat(at).strftime("%Y-%m-%d %H:%M UTC")
        except (ValueError, TypeError):
            at_disp = at
        model = r.get("model") or r.get("label") or "—"
        runs_display.append({
            "at": at_disp,
            "model": str(model).split("/")[-1],
            "new_claims": r.get("new_claims", 0),
            "total_claims": r.get("total_claims"),
            "resumed": r.get("resumed", False),
        })
    # Fallback for runs recorded before per-run history existed: synthesize one row
    # from the snapshot + the stored model label so the author still sees it.
    if not runs_display and snapshot.get("claims"):
        label = interview_data.get("_autoresearch_model") or "—"
        runs_display.append({
            "at": "(earlier run)",
            "model": str(label).split("·")[-1].split("(")[0].strip().split("/")[-1] or label,
            "new_claims": len(snapshot["claims"]),
            "total_claims": len(snapshot["claims"]),
            "resumed": False,
        })

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
            "model_roster": interview_data.get("_autoresearch_models") or (
                [interview_data["_autoresearch_model"]] if interview_data.get("_autoresearch_model") else []),
            "runs": runs_display,
        },
    )
