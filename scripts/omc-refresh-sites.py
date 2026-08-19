#!/usr/bin/env python3
"""Re-skin already-published runs with a newer viz, without rerunning them.

The viz app is baked into each run's results at BUNDLE_VIZ_SITE, so a change to
the front end reaches only runs executed after the image carrying it. That is the
wrong shape for a change that is purely presentational — a mobile layout, a
default marker size — where the data is fine and only the app that draws it has
moved on.

The app is built once from the pipeline image at a given commit, then paired with
each run's own viz/ data and redeployed. Nothing is recomputed and no archive is
rewritten: the published site is replaced, the results are untouched.

Usage:
  omc-refresh-sites.py --commit latest --dry-run
  omc-refresh-sites.py --commit 32e41b6 --user public
  omc-refresh-sites.py --commit latest --slugs edcd4843 12d73930
"""
import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/opt/omc-platform/portal")
os.chdir("/opt/omc-platform/portal")

from sqlalchemy import select                                                # noqa: E402
from sqlalchemy.orm import attributes                                        # noqa: E402
from app.database import (async_session, Submission, SubmissionStatus,       # noqa: E402
                          PipelineType, User)
from app.microscape_deploy import deploy_submission                          # noqa: E402

IMAGE = "ghcr.io/rec3141/danaseq-illumina-amplicon"


def build_dist(commit: str, keep: Path | None = None) -> tuple[Path, str]:
    """Build the viz bundle inside the pipeline image; return (dist, built sha).

    The image ships /pipeline/viz with node_modules already installed, so this
    needs no network beyond the pull and produces exactly the app that commit
    would have produced.
    """
    tag = f"{IMAGE}:{commit}"
    print(f"[INFO] pulling {tag}")
    subprocess.run(["docker", "pull", "-q", tag], check=True, timeout=1800)

    sha = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "sh", tag, "-lc",
         "printenv DANASEQ_GIT_SHA"],
        capture_output=True, text=True, timeout=120).stdout.strip()
    print(f"[INFO] image built from {sha or 'unknown commit'}")

    out = keep or Path(tempfile.mkdtemp(prefix="omc-viz-dist-"))
    out.mkdir(parents=True, exist_ok=True)
    # The data lands in site/data at deploy time, so build without any: one
    # bundle serves every run.
    subprocess.run(
        ["docker", "run", "--rm", "-v", f"{out}:/out", "--entrypoint", "sh", tag, "-lc",
         "cd /pipeline/viz && rm -rf dist public/data && npx vite build >/dev/null 2>&1 "
         "&& cp -a dist/. /out/ && ls /out/index.html"],
        check=True, timeout=1800, capture_output=True)
    if not (out / "index.html").exists():
        raise SystemExit("build produced no index.html")
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"[INFO] built viz bundle: {size/1e6:.1f} MB at {out}")
    return out, sha


def _publish_bundle(dist: Path, sha: str) -> None:
    """Leave the built bundle where the offline download can find it.

    Every deployed run is wearing this app, so a download built from a run's own
    archive would hand back a different — usually older — one. Replaced whole
    via a rename, so a request landing mid-refresh sees one bundle or the other
    and never half of each.
    """
    from app.config import get_settings
    target = Path(get_settings().viz_bundle_dir)
    staging = target.with_name(target.name + ".incoming")
    previous = target.with_name(target.name + ".previous")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(dist, staging)
        (staging / "BUNDLE_SHA").write_text(f"{sha}\n")
        shutil.rmtree(previous, ignore_errors=True)
        if target.exists():
            target.rename(previous)
        staging.rename(target)
        shutil.rmtree(previous, ignore_errors=True)
        print(f"[INFO] published viz bundle to {target}")
    except Exception as exc:
        print(f"[WARN] could not publish viz bundle to {target}: {exc}")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--commit", default="latest",
                    help="image tag: 'latest' or a short commit sha")
    ap.add_argument("--user", help="only runs owned by this github_login")
    ap.add_argument("--slugs", nargs="*", help="only these runs")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only-deployed", action="store_true",
                    help="skip runs that have no viz url yet (do not publish new ones)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    async with async_session() as db:
        # The bundle this builds is the amplicon viz, which knows only how to
        # read an amplicon run's viz/ payload. Handed a MAG run it publishes an
        # app that finds nothing, over the top of whatever that run had before.
        q = select(Submission).where(
            Submission.deleted_at.is_(None),
            Submission.status == SubmissionStatus.RESULTS_READY,
            Submission.pipeline == PipelineType.ILLUMINA_AMPLICON,
        ).order_by(Submission.created_at.desc())
        if a.slugs:
            q = q.where(Submission.slug.in_(a.slugs))
        if a.user:
            u = (await db.execute(select(User).where(User.github_login == a.user))).scalar_one_or_none()
            if not u:
                sys.exit(f"no such user: {a.user}")
            q = q.where(Submission.user_id == u.id)
        subs = (await db.execute(q)).scalars().all()
        if a.slugs:
            asked = set(a.slugs) - {x.slug for x in subs}
            if asked:
                print(f"[WARN] not amplicon runs, skipped: {' '.join(sorted(asked))}")

    if a.only_deployed:
        subs = [s for s in subs if (s.sample_metadata or {}).get("microscape_viz_url")]
    if a.limit:
        subs = subs[:a.limit]

    print(f"[INFO] {len(subs)} run(s) to refresh")
    if a.dry_run:
        for s in subs:
            has = "deployed" if (s.sample_metadata or {}).get("microscape_viz_url") else "not deployed"
            print(f"  would refresh {s.slug}  {s.bioproject_accession}  ({has})")
        return 0
    if not subs:
        return 0

    dist, sha = build_dist(a.commit)
    _publish_bundle(dist, sha)
    ok = failed = 0
    try:
        async with async_session() as db:
            for s in subs:
                sub = (await db.execute(
                    select(Submission).where(Submission.slug == s.slug))).scalar_one()
                user = await db.get(User, sub.user_id)
                try:
                    url = await deploy_submission(sub, user, site_source=dist)
                except Exception as exc:
                    url = None
                    print(f"  {sub.slug}: RAISED {type(exc).__name__}: {exc}")
                if url:
                    meta = dict(sub.sample_metadata or {})
                    meta["microscape_viz_url"] = url
                    # Which viz a published site is running, so a stale skin is
                    # visible rather than guessed at.
                    meta["microscape_viz_build"] = sha[:7] if sha else a.commit
                    sub.sample_metadata = meta
                    attributes.flag_modified(sub, "sample_metadata")
                    await db.commit()
                    ok += 1
                    print(f"  {sub.slug}: {url}")
                else:
                    failed += 1
                    print(f"  {sub.slug}: no url returned")
    finally:
        shutil.rmtree(dist, ignore_errors=True)

    print(f"\n[INFO] refreshed {ok}, failed {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
