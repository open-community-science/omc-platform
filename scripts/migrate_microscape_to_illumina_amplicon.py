#!/usr/bin/env python3
"""Rewrite stored pipeline values after MICROSCAPE -> ILLUMINA_AMPLICON.

`Submission.pipeline` is `Column(Enum(PipelineType))` with no values_callable,
so SQLAlchemy stores the enum MEMBER NAME -- 'MICROSCAPE', not 'microscape'.
Renaming the member therefore orphans every existing amplicon submission: the
row still says MICROSCAPE and no longer maps to anything, and loading it raises
LookupError. This rewrites those rows.

Run it with the portal stopped, between pulling the rename and restarting:

    sudo systemctl stop omc-portal omc-download-worker
    python3 scripts/migrate_microscape_to_illumina_amplicon.py
    sudo systemctl start omc-portal omc-download-worker

Idempotent: a second run finds nothing to do. --dry-run reports without writing.
"""
import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

OLD, NEW = "MICROSCAPE", "ILLUMINA_AMPLICON"
DEFAULT_DB = "/opt/omc-platform/portal/omc.db"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB, help=f"sqlite path (default {DEFAULT_DB})")
    ap.add_argument("--dry-run", action="store_true", help="report, change nothing")
    ap.add_argument("--no-backup", action="store_true", help="skip the .bak copy")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"[ERROR] no such database: {db}", file=sys.stderr)
        return 1

    con = sqlite3.connect(str(db))
    try:
        before = dict(con.execute(
            "SELECT pipeline, COUNT(*) FROM submissions GROUP BY pipeline").fetchall())
        print("Current pipeline values:")
        for k, v in sorted(before.items(), key=lambda kv: (kv[0] or "")):
            print(f"  {str(k):20} {v}")

        n = before.get(OLD, 0)
        if n == 0:
            print(f"\nNothing to migrate: no rows with {OLD}.")
            return 0
        if args.dry_run:
            print(f"\n[DRY RUN] would rewrite {n} row(s): {OLD} -> {NEW}")
            return 0

        if not args.no_backup:
            bak = db.with_suffix(db.suffix + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(db, bak)
            print(f"\nBackup: {bak}")

        cur = con.execute(
            "UPDATE submissions SET pipeline = ? WHERE pipeline = ?", (NEW, OLD))
        con.commit()
        print(f"Rewrote {cur.rowcount} row(s): {OLD} -> {NEW}")

        after = dict(con.execute(
            "SELECT pipeline, COUNT(*) FROM submissions GROUP BY pipeline").fetchall())
        leftover = after.get(OLD, 0)
        if leftover:
            print(f"[ERROR] {leftover} row(s) still say {OLD}", file=sys.stderr)
            return 1
        print(f"Verified: {after.get(NEW, 0)} row(s) now {NEW}, none left as {OLD}.")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
