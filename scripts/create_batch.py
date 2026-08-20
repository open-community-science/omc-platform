#!/usr/bin/env python3
"""Create OMC submissions for a list of BioProjects, using the portal's own code paths.

Mirrors what the UI does, in the same order:
  create draft -> resolve accession (metadata + MIxS records + auto pipeline)
             -> select the Illumina/AMPLICON/PAIRED breakdown rows
  and, with --submit, the /submit step: QUEUED + _launch_download (which resolves
  primers before building the download job).

Deliberately two phases: drafts are cheap and inspectable, queueing is not.

Usage:
  python3 create_batch.py --accessions PRJNA1 PRJNA2      # create drafts
  python3 create_batch.py --submit SLUG1 SLUG2            # queue existing drafts
  python3 create_batch.py --accessions ... --dry-run      # resolve only, write nothing
"""
import argparse, asyncio, os, sys
from pathlib import Path

# Import the portal package from this checkout, so the script runs wherever the
# repo is rather than only from the deploy path. Run it with the portal's venv:
#   /opt/omc-platform/.venv/bin/python scripts/create_batch.py ...
PORTAL = Path(__file__).resolve().parent.parent / "portal"
sys.path.insert(0, str(PORTAL))

# settings.database_url is "sqlite+aiosqlite:///./omc.db" -- relative to the
# process's working directory, which is why the service sets WorkingDirectory to
# portal/. Run from anywhere else and the engine happily opens an EMPTY database
# and every query fails with "no such table: submissions". Match the service
# rather than require the caller to remember.
os.chdir(PORTAL)

from sqlalchemy import select                                   # noqa: E402
from app.database import async_session, Submission, SubmissionStatus, PipelineType, User  # noqa: E402
from app.config import settings                                                 # noqa: E402
from app.sra_metadata import resolve_to_bioproject, fetch_sample_metadata           # noqa: E402
from app.submissions import _auto_pipeline, _launch_download                        # noqa: E402
from sqlalchemy.orm import attributes                                               # noqa: E402

def wanted(row: dict) -> bool:
    """The rows this batch is about: paired-end Illumina amplicon."""
    return ("illumina" in (row.get("platform") or "").lower()
            and (row.get("strategy") or "").upper() == "AMPLICON"
            and (row.get("layout") or "").upper() == "PAIRED")


async def create_one(acc: str, dry: bool, user_id: int) -> str | None:
    meta = await resolve_to_bioproject(acc)
    if "error" in meta:
        print(f"  {acc}: RESOLVE FAILED — {meta['error']}")
        return None
    bp = meta.get("accession", acc)
    breakdown = meta.get("breakdown", []) or []
    idx = [i for i, r in enumerate(breakdown) if wanted(r)]
    runs = sum(breakdown[i]["runs"] for i in idx)
    title = meta.get("study_title") or meta.get("title") or f"Analysis of {bp}"

    if not idx:
        print(f"  {acc}: no paired Illumina amplicon rows in breakdown — SKIPPED")
        return None

    if dry:
        print(f"  {acc} -> {bp} | {runs} runs in {len(idx)}/{len(breakdown)} rows | {title[:52]}")
        return None

    records = await fetch_sample_metadata(bp)
    if records:
        meta["sample_records"] = records

    async with async_session() as db:
        sub = Submission(user_id=user_id, title=title, status=SubmissionStatus.DRAFT)
        db.add(sub)
        await db.commit()
        await db.refresh(sub)

        sub.bioproject_accession = bp
        sub.sra_accession = acc if acc != bp else None
        sub.sample_metadata = meta
        sub.title = title
        sub.selected_runs = [breakdown[i] for i in idx]
        sub.pipeline = _auto_pipeline(sub.selected_runs)
        attributes.flag_modified(sub, "sample_metadata")
        attributes.flag_modified(sub, "selected_runs")
        await db.commit()
        print(f"  {acc} -> {bp} | {sub.slug} | {runs} runs | {sub.pipeline.value} | "
              f"{len(records) if records else 0} MIxS records | {title[:44]}")
        return sub.slug


async def submit_one(slug: str) -> None:
    from datetime import datetime
    async with async_session() as db:
        sub = (await db.execute(select(Submission).where(Submission.slug == slug))).scalar_one_or_none()
        if not sub:
            print(f"  {slug}: not found"); return
        if sub.status != SubmissionStatus.DRAFT:
            print(f"  {slug}: not a draft ({sub.status.value}) — skipped"); return
        if not sub.bioproject_accession or not sub.selected_runs:
            print(f"  {slug}: missing accession or selection — skipped"); return
        sub.status = SubmissionStatus.QUEUED
        sub.submitted_at = datetime.utcnow()
        await db.commit()
    await _launch_download(slug)     # resolves primers, then builds the download job
    print(f"  {slug}: QUEUED + download launched")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--accessions", nargs="*", default=[])
    ap.add_argument("--submit", nargs="*", default=[])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--user-id", type=int, default=None,
                    help="owning user (default: the public account, so the runs "
                         "appear on /public)")
    a = ap.parse_args()

    # This script exists to run public BioProjects, and /public lists exactly the
    # submissions the public account owns. Defaulting to any other account
    # produces runs that complete, deploy their viz, and appear on no page an
    # anonymous visitor can reach.
    if a.user_id is None:
        async with async_session() as db:
            row = await db.execute(
                select(User).where(User.github_login == settings.public_user_login))
            u = row.scalar_one_or_none()
        if u is None:
            print(f"no {settings.public_user_login!r} account exists — pass "
                  f"--user-id to say who should own these", file=sys.stderr)
            return
        a.user_id = u.id
        print(f"owner: {settings.public_user_login} (user {u.id})")

    if a.accessions:
        print(f"resolving {len(a.accessions)} accession(s):")
        slugs = []
        for acc in a.accessions:
            try:
                s = await create_one(acc, a.dry_run, a.user_id)
                if s: slugs.append(s)
            except Exception as e:
                print(f"  {acc}: ERROR {type(e).__name__}: {e}")
        if slugs:
            print("\nslugs: " + " ".join(slugs))

    for slug in a.submit:
        try:
            await submit_one(slug)
        except Exception as e:
            print(f"  {slug}: ERROR {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
