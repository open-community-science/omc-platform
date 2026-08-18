"""Review endpoints - trigger AI reviews on manuscripts.

Reviews always produce GitHub PRs on the paper's repository.
Each review type (statistical, methodological, clarity) gets its own PR
with structured comments, matching OMC's git-native review model.
"""
import asyncio
import json
import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import attributes
from sqlalchemy import select

from .config import get_settings
from .database import get_db, async_session, Submission, User
from .auth import require_user
from .run_registry import Run, registry, stream_run, status_payload, sse_response
from .study_facts import build_study_facts

router = APIRouter(prefix="/reviews", tags=["reviews"])
settings = get_settings()
logger = logging.getLogger(__name__)

# Detached, disconnect-surviving runs (see run_registry). slug -> Run.
_MS_RUNS = registry("manuscript")     # manuscript drafting
_REVIEW_RUNS = registry("reviews")    # AI review generation


async def _get_llm_config(user_id: int) -> dict:
    """Resolve the LLM config for a user's chosen backend.

    Honours the explicit local / shared-admin / personal choice made in
    /settings (see llm_backends.resolve_llm) instead of silently preferring
    whichever key happens to exist. The returned dict carries a "label" naming
    the backend + model, which callers surface alongside generated output.
    """
    from sqlalchemy import select as _select
    from .database import User as _User
    from .llm_backends import resolve_llm
    from .database import async_session as _session
    async with _session() as _db:
        target = (await _db.execute(_select(_User).where(_User.id == user_id))).scalar_one_or_none()
    return await resolve_llm(target)


def _dir_has_content(path) -> bool:
    """Check if a directory exists and has files (not just an empty mountpoint)."""
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return False
    try:
        return any(p.iterdir())
    except OSError:
        return False


def _format_review_as_markdown(review: dict) -> str:
    """Convert a structured review dict into a readable markdown comment."""
    review_type = review.get("review_type", "general")
    lines = [f"## AI Review: {review_type.title()}\n"]

    if review.get("summary"):
        lines.append(f"**Summary:** {review['summary']}\n")

    for comment in review.get("comments", []):
        severity = comment.get("severity", "suggestion")
        confidence = comment.get("confidence", 0.5)
        badge = {"critical": "🔴", "major": "🟠", "minor": "🟡", "suggestion": "💡"}.get(severity, "💡")

        lines.append(f"### {badge} [{severity.upper()}] {comment.get('issue', '')}")
        lines.append(f"**Section:** {comment.get('section', 'general')} · **Confidence:** {confidence:.0%}\n")
        lines.append(comment.get("detail", ""))
        lines.append("")

    return "\n".join(lines)


@router.post("/{slug}/run")
async def run_reviews(
    slug: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Run all AI review agents on a submission's manuscript.

    Each review agent produces a GitHub PR on the paper repo.
    Requires the submission to have a generated manuscript and a GitHub repo.
    """
    from ai.review_agents import run_all_reviews
    from ai.pipeline_parser import parse_pipeline_outputs
    from .github_integration import create_review_pr
    from pathlib import Path

    stmt = select(Submission).where(
        Submission.slug == slug,
        Submission.user_id == user.id,
    )
    result = await db.execute(stmt)
    submission = result.scalar_one_or_none()
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")

    interview_data = submission.interview_data or {}
    manuscript = interview_data.get("_manuscript")
    if not manuscript:
        raise HTTPException(
            status_code=400,
            detail="No manuscript found. Generate manuscript first.",
        )

    has_github_repo = bool(submission.github_repo)

    # Parse pipeline outputs for statistical review — mount sqsh if needed
    pipeline_outputs = {}
    try:
        from .staging import get_results_path
        from .sessions import _sqsh_mount, SQSH_MOUNT_BASE

        sqsh_mount = SQSH_MOUNT_BASE / submission.slug
        sqsh_path = get_results_path(submission.slug)

        if sqsh_path and not _dir_has_content(sqsh_mount):
            try:
                sqsh_mount = await _sqsh_mount(submission.slug, sqsh_path)
            except Exception as e:
                logger.warning(f"Could not mount sqsh for {submission.slug}: {e}")

        for base in [sqsh_mount, Path(settings.local_download_path) / submission.slug]:
            if _dir_has_content(base):
                pipeline_outputs = parse_pipeline_outputs(
                    submission.pipeline.value, base
                )
                break
    except Exception as e:
        logger.warning(f"Pipeline output parsing failed for {submission.slug}: {e}")

    # Everything we know about the study, not just {pipeline, accession} — a
    # reviewer has to be able to check a Methods claim about the assay against
    # what was actually run (issue #56).
    pipeline_config = build_study_facts(submission)

    llm = await _get_llm_config(user.id)
    reviews = await run_all_reviews(
        manuscript,
        pipeline_outputs,
        pipeline_config,
        base_url=llm["base_url"],
        api_key=llm["api_key"],
        model=settings.role_model("review", llm["model"]),
    )

    # Create PRs if the paper has a GitHub repo
    pr_urls = []
    if has_github_repo:
        # e.g. "https://github.com/omc-papers/paper-0001" → "omc-papers/paper-0001"
        repo_full_name = "/".join(submission.github_repo.rstrip("/").split("/")[-2:])

        for review in reviews:
            review_type = review.get("review_type", "general")
            body_md = _format_review_as_markdown(review)

            try:
                pr_url = await create_review_pr(
                    repo_full_name,
                    [{"body": body_md}],
                    review_type,
                )
                pr_urls.append({"review_type": review_type, "pr_url": pr_url})
                logger.info(f"Created review PR: {pr_url}")
            except Exception as e:
                logger.error(f"Failed to create {review_type} review PR: {e}")
                pr_urls.append({"review_type": review_type, "error": str(e)})
    else:
        logger.info(f"No GitHub repo for {slug} — reviews saved without PRs")

    # Store review results and PR URLs in interview_data. Stamp the model that
    # produced the reviews (attribution: whoever does something leaves its stamp).
    interview_data["_reviews"] = reviews
    interview_data["_review_prs"] = pr_urls
    interview_data["_review_model"] = llm.get("label")
    interview_data["_review_backend"] = llm.get("backend")
    interview_data["_review_models"] = {"review": settings.role_model("review", llm["model"])}

    # Optional revise pass (issue #20): feed review findings + deterministic
    # checks back into a section rewrite. Off by default and purely additive —
    # the original manuscript and the review PRs above are left untouched; the
    # revised draft is stored separately for the author to accept or discard.
    revise_log = None
    if settings.manuscript_revise_enabled:
        try:
            from ai.manuscript_generator import revise_manuscript
            revised, revise_log = await revise_manuscript(
                manuscript,
                reviews=reviews,
                results_data=pipeline_outputs,
                study_metadata=build_study_facts(submission),
                base_url=llm["base_url"],
                api_key=llm["api_key"],
                model=settings.role_model("draft", llm["model"]),
                max_passes=settings.manuscript_revise_max_passes,
            )
            interview_data["_manuscript_revised"] = revised
            interview_data["_revise_log"] = revise_log
            interview_data["_manuscript_revised_model"] = settings.role_model("draft", llm["model"])
        except Exception as e:
            logger.warning(f"Revise pass failed for {slug} (reviews preserved): {e}")

    submission.interview_data = interview_data
    attributes.flag_modified(submission, "interview_data")
    await db.commit()

    return {
        "slug": slug,
        "reviews": reviews,
        "pull_requests": pr_urls,
        "revise_log": revise_log,
    }


@router.post("/{slug}/run-stream")
async def run_reviews_stream(
    slug: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Run AI reviews with SSE progress — as a DETACHED, disconnect-surviving run.

    Same work as ``/run`` (review agents → PRs → optional revise → persist) but the
    worker owns the run: it completes and saves whether or not the browser stays,
    so navigating away no longer loses the reviews (and no more 'Do not close this
    page')."""
    from ai.review_agents import run_all_reviews
    from ai.pipeline_parser import parse_pipeline_outputs
    from .github_integration import create_review_pr
    from pathlib import Path

    stmt = select(Submission).where(Submission.slug == slug, Submission.user_id == user.id)
    submission = (await db.execute(stmt)).scalar_one_or_none()
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")
    manuscript = (submission.interview_data or {}).get("_manuscript")
    if not manuscript:
        raise HTTPException(status_code=400, detail="No manuscript found. Generate manuscript first.")

    # Capture submission fields to locals — the worker outlives this request scope.
    sub_slug = submission.slug
    sub_id = submission.id
    sub_pipeline = submission.pipeline.value
    sub_accession = submission.bioproject_accession
    sub_github_repo = submission.github_repo
    sub_sample_meta = dict(submission.sample_metadata or {})
    has_github_repo = bool(sub_github_repo)

    # Parse pipeline outputs for the statistical reviewer (request scope; mount sqsh).
    pipeline_outputs = {}
    try:
        from .staging import get_results_path
        from .sessions import _sqsh_mount, SQSH_MOUNT_BASE
        sqsh_mount = SQSH_MOUNT_BASE / sub_slug
        sqsh_path = get_results_path(sub_slug)
        if sqsh_path and not _dir_has_content(sqsh_mount):
            try:
                sqsh_mount = await _sqsh_mount(sub_slug, sqsh_path)
            except Exception as e:
                logger.warning(f"Could not mount sqsh for {sub_slug}: {e}")
        for base in [sqsh_mount, Path(settings.local_download_path) / sub_slug]:
            if _dir_has_content(base):
                pipeline_outputs = parse_pipeline_outputs(sub_pipeline, base)
                break
    except Exception as e:
        logger.warning(f"Pipeline output parsing failed for {sub_slug}: {e}")
    pipeline_config = {"pipeline": sub_pipeline, "accession": sub_accession}
    llm = await _get_llm_config(user.id)

    existing = _REVIEW_RUNS.get(sub_slug)
    if existing and not existing.done:
        return sse_response(stream_run(existing))
    run = Run()
    _REVIEW_RUNS[sub_slug] = run

    async def worker():
        run.emit("start", f"Using {llm['label']}")
        try:
            run.emit("review", "running AI review agents")
            reviews = await run_all_reviews(
                manuscript, pipeline_outputs, pipeline_config,
                base_url=llm["base_url"], api_key=llm["api_key"],
                model=settings.role_model("review", llm["model"]))

            pr_urls = []
            if has_github_repo:
                repo_full_name = "/".join(sub_github_repo.rstrip("/").split("/")[-2:])
                for review in reviews:
                    rt = review.get("review_type", "general")
                    run.emit("review", f"posting {rt} review PR")
                    try:
                        pr_url = await create_review_pr(
                            repo_full_name, [{"body": _format_review_as_markdown(review)}], rt)
                        pr_urls.append({"review_type": rt, "pr_url": pr_url})
                    except Exception as e:
                        logger.error(f"Failed to create {rt} review PR: {e}")
                        pr_urls.append({"review_type": rt, "error": str(e)})

            revised, revise_log = None, None
            if settings.manuscript_revise_enabled:
                run.emit("review", "revising manuscript from review findings")
                try:
                    from ai.manuscript_generator import revise_manuscript
                    revised, revise_log = await revise_manuscript(
                        manuscript, reviews=reviews, results_data=pipeline_outputs,
                        study_metadata=sub_sample_meta,
                        base_url=llm["base_url"], api_key=llm["api_key"],
                        model=settings.role_model("draft", llm["model"]),
                        max_passes=settings.manuscript_revise_max_passes)
                except Exception as e:
                    logger.warning(f"Revise pass failed for {sub_slug} (reviews preserved): {e}")
        except Exception as e:
            logger.exception(f"Reviews failed for {sub_slug}")
            run.emit("error", str(e))
            run.finish("error"); return

        # Persist — ALWAYS. Re-read interview_data in a fresh session and merge.
        run.emit("start", "Saving reviews...")
        try:
            async with async_session() as save_db:
                sub = (await save_db.execute(
                    select(Submission).where(Submission.id == sub_id))).scalar_one()
                idata = dict(sub.interview_data or {})
                idata["_reviews"] = reviews
                idata["_review_prs"] = pr_urls
                idata["_review_model"] = llm.get("label")
                idata["_review_backend"] = llm.get("backend")
                idata["_review_models"] = {"review": settings.role_model("review", llm["model"])}
                if revised is not None:
                    idata["_manuscript_revised"] = revised
                    idata["_revise_log"] = revise_log
                    idata["_manuscript_revised_model"] = settings.role_model("draft", llm["model"])
                sub.interview_data = idata
                attributes.flag_modified(sub, "interview_data")
                await save_db.commit()
        except Exception as e:
            logger.exception(f"Reviews save failed for {sub_slug}")
            run.emit("error", f"Save failed: {e}")
            run.finish("error"); return

        n_pr = len([p for p in pr_urls if p.get("pr_url")])
        run.push({"event": "complete",
                  "detail": f"Reviews complete — {len(reviews)} reviews, {n_pr} PRs", "reload": True})
        run.finish("complete")

    asyncio.create_task(worker())
    return sse_response(stream_run(run))


@router.get("/{slug}/run-status")
async def reviews_status(
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Is a review run in flight? Lets the page reconnect after a navigation."""
    from .auth import get_current_user
    user = await get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return status_payload(_REVIEW_RUNS.get(slug))


@router.post("/{slug}/generate")
async def generate_manuscript(
    slug: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Generate manuscript from pipeline outputs and interview data.

    Stores the generated sections in interview_data._manuscript.
    """
    from ai.manuscript_generator import generate_manuscript_draft
    from ai.pipeline_parser import parse_pipeline_outputs
    from sqlalchemy.orm import attributes
    from pathlib import Path

    stmt = select(Submission).where(
        Submission.slug == slug,
        Submission.user_id == user.id,
    )
    result = await db.execute(stmt)
    submission = result.scalar_one_or_none()
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")

    # Parse pipeline outputs — mount sqsh if needed, check loose files
    pipeline_outputs = {}
    results_base = None
    try:
        from .staging import get_results_path
        from .sessions import _sqsh_mount, SQSH_MOUNT_BASE

        sqsh_mount = SQSH_MOUNT_BASE / submission.slug
        sqsh_path = get_results_path(submission.slug)

        # If sqsh exists but not mounted, mount it
        if sqsh_path and not _dir_has_content(sqsh_mount):
            try:
                sqsh_mount = await _sqsh_mount(submission.slug, sqsh_path)
            except Exception as e:
                logger.warning(f"Could not mount sqsh for {submission.slug}: {e}")

        for base in [sqsh_mount, Path(settings.local_download_path) / submission.slug]:
            if _dir_has_content(base):
                results_base = base
                pipeline_outputs = parse_pipeline_outputs(
                    submission.pipeline.value, base
                )
                break
    except Exception as e:
        logger.warning(f"Pipeline output parsing failed for {submission.slug}: {e}")

    # Generate figures from pipeline data
    figures_json = {}
    try:
        from ai.figure_generator import generate_figures
        figures_json = generate_figures(pipeline_outputs, submission.pipeline.value)
    except Exception:
        pass

    interview_data = dict(submission.interview_data or {})

    llm = await _get_llm_config(user.id)
    sections = await generate_manuscript_draft(
        pipeline_outputs=pipeline_outputs,
        interview_data=interview_data,
        pipeline_type=submission.pipeline.value,
        bioproject_accession=submission.bioproject_accession,
        study_metadata=build_study_facts(submission),
        base_url=llm["base_url"],
        api_key=llm["api_key"],
        model=settings.role_model("draft", llm["model"]),
        cite_model=settings.role_model("cite", llm["model"]),
        outline_first=settings.manuscript_outline_first,
    )

    # Store manuscript in interview_data (but don't publish yet)
    interview_data["_manuscript"] = sections
    # Provenance (#16): the backend label PLUS the per-role model map, since drafting
    # and citation resolution can use different models (role_model draft/cite).
    interview_data["_manuscript_model"] = llm.get("label")
    interview_data["_manuscript_backend"] = llm.get("backend")
    interview_data["_manuscript_models"] = {
        "draft": settings.role_model("draft", llm["model"]),
        "cite": settings.role_model("cite", llm["model"]),
    }
    submission.interview_data = interview_data
    attributes.flag_modified(submission, "interview_data")
    await db.commit()

    return {
        "slug": slug,
        "sections": {k: len(v) for k, v in sections.items()},
        "manuscript": sections,
        "preview_url": f"/submissions/{slug}/manuscript",
    }


@router.post("/{slug}/generate-stream")
async def generate_manuscript_stream(
    slug: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Generate manuscript with SSE progress streaming."""
    from ai.manuscript_generator import generate_manuscript_draft
    from ai.pipeline_parser import parse_pipeline_outputs
    from pathlib import Path

    stmt = select(Submission).where(
        Submission.slug == slug,
        Submission.user_id == user.id,
    )
    result = await db.execute(stmt)
    submission = result.scalar_one_or_none()
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")

    # Capture submission data before we leave the db session scope
    sub_slug = submission.slug
    sub_pipeline = submission.pipeline.value
    sub_accession = submission.bioproject_accession
    sub_interview = dict(submission.interview_data or {})
    # Grounds the paper's subject matter — without it the model invents a study system.
    # Snapshotted inside the db session; the drafting task runs after it closes.
    sub_study_meta = build_study_facts(submission)
    sub_github_repo = submission.github_repo
    sub_title = submission.title
    sub_id = submission.id

    # Resolve LLM config (OpenRouter if user has it, else local)
    llm = await _get_llm_config(user.id)

    # Parse pipeline outputs
    pipeline_outputs = {}
    try:
        from .staging import get_results_path
        from .sessions import _sqsh_mount, SQSH_MOUNT_BASE

        sqsh_mount = SQSH_MOUNT_BASE / sub_slug
        sqsh_path = get_results_path(sub_slug)

        if sqsh_path and not _dir_has_content(sqsh_mount):
            try:
                sqsh_mount = await _sqsh_mount(sub_slug, sqsh_path)
            except Exception:
                pass

        for base in [sqsh_mount, Path(settings.local_download_path) / sub_slug]:
            if _dir_has_content(base):
                pipeline_outputs = parse_pipeline_outputs(sub_pipeline, base)
                break
    except Exception:
        pass

    # Already generating for this submission? Attach (reconnect) instead of starting
    # a second run — so navigating back resumes the live progress.
    existing = _MS_RUNS.get(sub_slug)
    if existing and not existing.done:
        return sse_response(stream_run(existing))

    run = Run()
    _MS_RUNS[sub_slug] = run

    async def worker():
        """Detached: drafts the manuscript and PERSISTS regardless of any listener,
        so navigating away no longer discards the draft."""
        async def on_progress(event, detail):
            run.emit(event, detail)

        run.emit("start", f"Using {llm['label']}")
        try:
            sections = await generate_manuscript_draft(
                pipeline_outputs=pipeline_outputs,
                interview_data=sub_interview,
                pipeline_type=sub_pipeline,
                bioproject_accession=sub_accession,
                study_metadata=sub_study_meta,
                base_url=llm["base_url"],
                api_key=llm["api_key"],
                model=settings.role_model("draft", llm["model"]),
                cite_model=settings.role_model("cite", llm["model"]),
                outline_first=settings.manuscript_outline_first,
                on_progress=on_progress,
            )
        except Exception as e:
            logger.exception(f"Manuscript generation failed for {sub_slug}")
            run.emit("error", str(e))
            run.finish("error"); return

        # Persist — ALWAYS, even if no client is still connected.
        run.emit("start", "Saving manuscript...")
        try:
            interview_data = dict(sub_interview)
            interview_data["_manuscript"] = sections
            # Provenance (#16): backend label PLUS the per-role model map (drafting
            # and citation resolution can use different models).
            interview_data["_manuscript_model"] = llm.get("label")
            interview_data["_manuscript_backend"] = llm.get("backend")
            interview_data["_manuscript_models"] = {
                "draft": settings.role_model("draft", llm["model"]),
                "cite": settings.role_model("cite", llm["model"]),
            }
            async with async_session() as save_db:
                save_stmt = select(Submission).where(Submission.id == sub_id)
                save_result = await save_db.execute(save_stmt)
                sub = save_result.scalar_one()
                sub.interview_data = interview_data
                attributes.flag_modified(sub, "interview_data")
                await save_db.commit()
        except Exception as e:
            logger.exception(f"Manuscript save failed for {sub_slug}")
            run.emit("error", f"Save failed: {e}")
            run.finish("error"); return

        preview_url = f"/submissions/{sub_slug}/manuscript"
        run.push({"event": "complete",
                  "detail": "Manuscript saved — redirecting to preview", "preview_url": preview_url})
        run.finish("complete", result_url=preview_url)

    asyncio.create_task(worker())
    return sse_response(stream_run(run))


@router.get("/{slug}/generate-status")
async def manuscript_status(
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Is a manuscript generation in flight? Lets the page reconnect after a
    navigation instead of losing it."""
    from .auth import get_current_user
    user = await get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return status_payload(_MS_RUNS.get(slug))


@router.post("/{slug}/publish")
async def publish_manuscript(
    slug: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Publish the manuscript to a GitHub paper repo.

    Only called after the user has previewed and is ready to commit.
    Creates the repo, pushes all files, enables GitHub Pages.
    """
    from .github_integration import create_paper_repo_from_files
    from ai.pipeline_parser import parse_pipeline_outputs
    from pathlib import Path

    stmt = select(Submission).where(
        Submission.slug == slug,
        Submission.user_id == user.id,
    )
    result = await db.execute(stmt)
    submission = result.scalar_one_or_none()
    if not submission:
        raise HTTPException(404, "Submission not found")

    interview_data = submission.interview_data or {}
    manuscript = interview_data.get("_manuscript")
    if not manuscript:
        raise HTTPException(400, "No manuscript to publish. Generate one first.")

    # Generate figures
    pipeline_outputs = {}
    try:
        from .staging import get_results_path
        from .sessions import _sqsh_mount, SQSH_MOUNT_BASE

        sqsh_mount = SQSH_MOUNT_BASE / submission.slug
        sqsh_path = get_results_path(submission.slug)
        if sqsh_path and not _dir_has_content(sqsh_mount):
            try:
                sqsh_mount = await _sqsh_mount(submission.slug, sqsh_path)
            except Exception:
                pass
        for base in [sqsh_mount, Path(settings.local_download_path) / submission.slug]:
            if _dir_has_content(base):
                pipeline_outputs = parse_pipeline_outputs(submission.pipeline.value, base)
                break
    except Exception:
        pass

    figures_json = {}
    try:
        from ai.figure_generator import generate_figures
        figures_json = generate_figures(pipeline_outputs, submission.pipeline.value)
    except Exception:
        pass

    files = _manuscript_to_files(manuscript, submission, figures_json)

    try:
        repo_url = await create_paper_repo_from_files(submission, files)
        submission.github_repo = repo_url
        attributes.flag_modified(submission, "interview_data")
        await db.commit()
        logger.info(f"Published manuscript for {slug} to {repo_url}")
        return {"slug": slug, "github_repo": repo_url}
    except Exception as e:
        logger.error(f"Publish failed for {slug}: {e}")
        raise HTTPException(500, f"Failed to publish: {e}")


def _sanitize_for_latex(text: str) -> str:
    """Sanitize AI-generated text so it compiles under pdflatex.

    Strategy: find contiguous runs of LaTeX commands (backslash sequences
    with nested braces) and wrap them in $...$. Also escape bare underscores.
    """
    import re

    # Split on existing math delimiters to avoid double-wrapping
    parts = re.split(r'(\$\$.*?\$\$|\$[^$]+?\$)', text, flags=re.DOTALL)
    result = []

    for i, part in enumerate(parts):
        if i % 2 == 1:
            # Inside existing math delimiters — leave alone
            result.append(part)
        else:
            # Outside math — find and wrap bare LaTeX expressions
            part = _wrap_bare_latex(part)
            # Escape bare underscores in prose (not URLs, not already escaped)
            part = re.sub(r'(?<!\\)(?<!/)(\w)_(\w)', r'\1\\_\2', part)
            result.append(part)

    return "".join(result)


def _wrap_bare_latex(text: str) -> str:
    """Find contiguous LaTeX command sequences and wrap them in $...$."""
    import re

    # Match a LaTeX expression: starts with \command, may have {nested} args,
    # subscripts, superscripts, and more \commands chained together
    LATEX_CMD = re.compile(
        r'(\\(?:[a-zA-Z]+))'   # \command
        r'((?:\s*[\{_^]'       # followed by { or _ or ^
        r'(?:[^{}]*'           # content without braces
        r'(?:\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}[^{}]*)*'  # nested braces up to 2 levels
        r')'
        r'}?)*)'               # repeated args
    )

    def _needs_wrapping(m):
        full = m.group(0)
        # Simple standalone commands like \alpha, \beta — always wrap
        # Complex expressions with braces — also wrap
        return f"${full}$"

    # Find runs that start with \ and contain LaTeX-like content
    # Greedily match: \cmd{...}{...} chains, \cmd_x^y, etc.
    result = LATEX_CMD.sub(_needs_wrapping, text)

    # Clean up any $$ that got created by adjacent wrappings: $\a$$\b$ → $\a\b$
    result = re.sub(r'\$\$(?![\s\n])', '', result)  # remove $$ at wrapping boundaries

    return result


def _manuscript_to_files(sections: dict, submission, figures: dict | None = None) -> dict:
    """Convert manuscript sections to Quarto paper repo files.

    Uses the paper-repo template structure for GitHub Actions rendering
    (HTML + PDF via Quarto).
    """
    import json as _json
    from datetime import date

    accession = getattr(submission, "bioproject_accession", "")
    pipeline = getattr(submission, "pipeline", None)
    pipeline_name = pipeline.value if pipeline else "unknown"
    title = submission.title or "Untitled"

    files = {}

    # Sanitize sections for LaTeX compatibility
    safe = {k: _sanitize_for_latex(v) if isinstance(v, str) else v for k, v in sections.items()}

    # Quarto manuscript (.qmd)
    abstract = safe.get("abstract", "").replace('"', '\\"')
    qmd = f"""---
title: "{title}"
author:
  - name: ""
    affiliations:
      - ""
date: "{date.today().isoformat()}"
abstract: |
  {abstract}
keywords:
  - microbial ecology
  - metagenomics
bibliography: references.bib
license: "CC BY 4.0"
citation:
  type: article
  container-title: "Open Microbial Community"
---

## Introduction

{safe.get('introduction', '')}

## Methods

{safe.get('methods', '')}

## Results

{safe.get('results', '')}

## Discussion

{safe.get('discussion', '')}

## Data Availability

All sequence data are available from the NCBI SRA under accession [{accession}](https://www.ncbi.nlm.nih.gov/bioproject/{accession}). Analysis code, results, and interactive figures are available in this repository. This paper was generated using the [Open Microbial Community](https://github.com/open-community-science/omc-platform) platform.

## References {{.unnumbered}}

::: {{#refs}}
:::
"""
    files["manuscript.qmd"] = qmd

    # Quarto project config
    files["_quarto.yml"] = """project:
  type: manuscript
  output-dir: docs

manuscript:
  article: manuscript.qmd

format:
  html:
    self-contained: true
    toc: true
    toc-depth: 3
    theme:
      light: cosmo
    css: styles.css
    code-fold: true
    fig-responsive: true
  pdf:
    documentclass: article
    geometry:
      - margin=1in
    fontsize: 11pt
    colorlinks: true
"""

    # BibTeX bibliography
    files["references.bib"] = sections.get("bibliography", "")

    # GitHub Actions workflow for auto-rendering
    files[".github/workflows/render.yml"] = """name: Render Paper

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

permissions:
  contents: write
  pages: write

jobs:
  render:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: quarto-dev/quarto-actions/setup@v2

      - name: Cache TinyTeX
        uses: actions/cache@v4
        id: tinytex-cache
        with:
          path: |
            ~/.TinyTeX
          key: tinytex-${{ runner.os }}-v1

      - name: Install TinyTeX
        if: steps.tinytex-cache.outputs.cache-hit != 'true'
        run: quarto install tinytex

      - name: Render HTML + PDF
        run: quarto render manuscript.qmd

      - name: Deploy to GitHub Pages
        if: github.ref == 'refs/heads/main'
        uses: peaceiris/actions-gh-pages@v3
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          publish_dir: ./docs
"""

    # Minimal CSS
    files["styles.css"] = """body { max-width: 800px; margin: 0 auto; font-family: serif; }
.abstract { font-style: italic; border-left: 3px solid #ccc; padding-left: 1em; }
"""

    # LICENSE
    files["LICENSE"] = "CC-BY-4.0\nhttps://creativecommons.org/licenses/by/4.0/\n"

    # README
    repo_name = f"micro-{getattr(submission, 'slug', '')}"
    org = settings.github_org
    pages_url = f"https://{org}.github.io/{repo_name}"

    files["README.md"] = f"""# {title}

OMC Paper — AI-assisted scientific manuscript

- **Read the paper:** [{pages_url}]({pages_url})
- **PDF:** [{pages_url}/manuscript.pdf]({pages_url}/manuscript.pdf)
- **BioProject:** [{accession}](https://www.ncbi.nlm.nih.gov/bioproject/{accession})
- **Pipeline:** {pipeline_name}
- **Generated by:** [OMC Platform](https://github.com/open-community-science/omc-platform)
- **License:** [CC-BY 4.0](https://creativecommons.org/licenses/by/4.0/)

## Structure

- **`main` branch** — manuscript source (Quarto `.qmd`, BibTeX, config)
- **[`gh-pages` branch](https://github.com/{org}/{repo_name}/tree/gh-pages)** — rendered output (HTML + PDF), auto-deployed by GitHub Actions

## Building

```bash
quarto render manuscript.qmd
```

Rendered output appears in `docs/`. GitHub Actions auto-renders on push to `main`.

## Review

Reviews are submitted as pull requests on this repository.
"""

    # Also save plain markdown for easy reading
    md_parts = [f"# {title}\n"]
    for name in ["abstract", "introduction", "methods", "results", "discussion"]:
        if name in sections:
            md_parts.append(f"## {name.title()}\n\n{sections[name]}\n")
    files["manuscript/manuscript.md"] = "\n".join(md_parts)

    # Interactive figures as Plotly JSON
    if figures:
        for fig_name, fig_data in figures.items():
            files[f"results/figures/{fig_name}.json"] = _json.dumps(fig_data, indent=2)

    # .omc/ provenance directory — full AI interaction history for training data
    interview_data = getattr(submission, "interview_data", None) or {}

    # Interview transcript (AI conversation with author)
    chat_history = interview_data.get("_chat_history", [])
    if chat_history:
        files[".omc/interview_transcript.json"] = _json.dumps(chat_history, indent=2)

    # First AI draft (before human edits) — this IS the training signal
    files[".omc/manuscript_v1.json"] = _json.dumps(sections, indent=2)

    # SRA/BioProject metadata
    sample_meta = getattr(submission, "sample_metadata", None)
    if sample_meta:
        files[".omc/submission_metadata.json"] = _json.dumps(sample_meta, indent=2, default=str)

    # Pipeline configuration
    files[".omc/pipeline_config.json"] = _json.dumps({
        "pipeline": pipeline_name,
        "bioproject_accession": accession,
        "submission_slug": getattr(submission, "slug", None),
        "generated_at": date.today().isoformat(),
        "platform": "open-community-science/omc-platform",
    }, indent=2)

    return files
