#!/usr/bin/env python3
"""Record what already-finished runs produced, which the pickup now does itself.

The dashboard shows each run's assay and ASV count from
`sample_metadata["assay_facts"]`, written when its results land. Runs that
finished before the pickup started recording it have nothing to show, and no
amount of page loading will fill it in: rendering a page deliberately does not
write. This does, once, for the runs that need it.

Reading a run costs a few milliseconds — four members out of its results
archive — so this is quick even over the whole set.

Usage:
  omc-backfill-assay-facts.py --dry-run
  omc-backfill-assay-facts.py
  omc-backfill-assay-facts.py --slugs edcd4843 90099196
  omc-backfill-assay-facts.py --force        # re-read runs already recorded
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, "/opt/omc-platform/portal")
os.chdir("/opt/omc-platform/portal")

from sqlalchemy import select                                            # noqa: E402
from sqlalchemy.orm import attributes                                    # noqa: E402
from app.database import async_session, Submission                       # noqa: E402
from app.microscape_deploy import assay_facts, results_archive_mtime     # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--slugs", nargs="*", help="only these runs")
    ap.add_argument("--force", action="store_true",
                    help="re-read runs whose facts are already current")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    async with async_session() as db:
        q = select(Submission).where(Submission.deleted_at.is_(None))
        if a.slugs:
            q = q.where(Submission.slug.in_(a.slugs))
        subs = (await db.execute(q.order_by(Submission.created_at))).scalars().all()

        filled = current = absent = failed = 0
        for sub in subs:
            meta = dict(sub.sample_metadata or {})
            cached = meta.get("assay_facts") or {}
            mtime = results_archive_mtime(sub.slug)
            if mtime is None:
                absent += 1
                continue
            if cached.get("mtime") == mtime and not a.force:
                current += 1
                continue
            if a.dry_run:
                print(f"  would read {sub.slug}  {sub.pipeline.value if sub.pipeline else '?'}")
                filled += 1
                continue
            facts = await asyncio.to_thread(assay_facts, sub.slug)
            if facts is None:
                failed += 1
                print(f"  {sub.slug}: unreadable")
                continue
            meta["assay_facts"] = facts
            sub.sample_metadata = meta
            attributes.flag_modified(sub, "sample_metadata")
            filled += 1
            target = ", ".join(
                " ".join(x for x in (v.get("gene"), v.get("region")) if x)
                for v in facts["assays"]) or "no assay recorded"
            print(f"  {sub.slug}: {target}; {facts['n_asvs']} ASVs")
        if not a.dry_run and filled:
            await db.commit()

    print(f"\n[INFO] {filled} recorded, {current} already current, "
          f"{absent} with no results archive, {failed} unreadable")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
