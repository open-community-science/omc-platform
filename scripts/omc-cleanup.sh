#!/bin/bash
# OMC Cleanup Cron — removes staging files for soft-deleted submissions
# Run hourly: 0 * * * * /opt/omc-platform/scripts/omc-cleanup.sh
#
# Cleans up:
#   1. Staging dirs for deleted submissions (deleted_at > 24h ago)
#   2. Kills orphaned download processes for deleted submissions
#   3. Removes staging dirs with no matching DB submission (orphans)
#
# If the staging volume is >90% full, grace period drops to 0 (immediate cleanup).

set -euo pipefail

DB_PATH="${OMC_DB_PATH:-/opt/omc-platform/portal/omc.db}"
STAGING_DIR="${OMC_STAGING_DIR:-/data/sra_downloads}"
GRACE_PERIOD=86400  # 24 hours — don't clean up until 24h after delete

# If disk is >90% full, skip the grace period
DISK_USE=$(df "$STAGING_DIR" --output=pcent 2>/dev/null | tail -1 | tr -d '% ')
if [ -n "$DISK_USE" ] && [ "$DISK_USE" -ge 90 ]; then
    echo "WARNING: Staging volume ${DISK_USE}% full — skipping grace period"
    GRACE_PERIOD=0
fi

if [ ! -f "$DB_PATH" ]; then
    echo "DB not found: $DB_PATH"
    exit 1
fi

if [ ! -d "$STAGING_DIR" ]; then
    exit 0
fi

# DB access goes through python3 rather than the sqlite3 CLI: the portal itself
# runs on python3, so it cannot be absent here while there is anything to clean
# up. Every rm below is gated on a slug being absent from the DB, which makes a
# query that returns nothing indistinguishable from "no submissions exist" —
# so a failed query must abort rather than fall through to the orphan branch.
PYTHON="${OMC_PYTHON:-python3}"

db_read() {
    # usage: db_read <sql> [params...] — one column per row, one row per line
    "$PYTHON" - "$DB_PATH" "$@" <<'PY'
import sqlite3, sys
db, sql, params = sys.argv[1], sys.argv[2], sys.argv[3:]
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
for row in con.execute(sql, params):
    print(row[0])
PY
}

db_write() {
    # usage: db_write <sql> [params...]
    "$PYTHON" - "$DB_PATH" "$@" <<'PY'
import sqlite3, sys
db, sql, params = sys.argv[1], sys.argv[2], sys.argv[3:]
con = sqlite3.connect(db)
con.execute(sql, params)
con.commit()
PY
}

echo "=== OMC Cleanup: $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

# Find soft-deleted submissions older than grace period
CUTOFF=$(date -u -d "-${GRACE_PERIOD} seconds" +%Y-%m-%dT%H:%M:%S 2>/dev/null || \
         date -u -v-${GRACE_PERIOD}S +%Y-%m-%dT%H:%M:%S)

if ! DELETED_SLUGS=$(db_read \
    "SELECT slug FROM submissions WHERE deleted_at IS NOT NULL AND deleted_at < ?" \
    "$CUTOFF"); then
    echo "FATAL: could not query deleted submissions" >&2
    exit 1
fi

# Also find staging dirs that have no matching submission at all (orphans)
if ! ACTIVE_SLUGS=$(db_read \
    "SELECT slug FROM submissions WHERE deleted_at IS NULL"); then
    echo "FATAL: could not query active submissions" >&2
    exit 1
fi

# A short read of the active list reads as "these submissions no longer exist"
# and sends their staging dirs to the orphan branch. Count separately and
# require the two to agree before anything is classified.
if ! ACTIVE_COUNT=$(db_read \
    "SELECT COUNT(*) FROM submissions WHERE deleted_at IS NULL"); then
    echo "FATAL: could not count active submissions" >&2
    exit 1
fi
ACTIVE_READ=$(printf '%s\n' "$ACTIVE_SLUGS" | grep -c . || true)
if [ "$ACTIVE_READ" -ne "$ACTIVE_COUNT" ]; then
    echo "FATAL: read $ACTIVE_READ active slugs but the table holds $ACTIVE_COUNT — refusing to classify orphans" >&2
    exit 1
fi

for dir in "$STAGING_DIR"/*/; do
    [ -d "$dir" ] || continue
    slug=$(basename "$dir")

    # Skip hidden dirs (.hpc_status, etc.)
    [[ "$slug" == .* ]] && continue

    # Check if this slug was explicitly deleted
    if echo "$DELETED_SLUGS" | grep -qx "$slug"; then
        echo "Cleaning deleted submission: $slug"

        # Kill any running download process
        if [ -f "$dir/download.sh" ]; then
            pgrep -f "download.sh.*$slug" | xargs -r kill 2>/dev/null || true
        fi

        rm -rf "$dir"
        echo "  Removed $dir"

        # Hard-delete the DB row now that files are cleaned up
        db_write "DELETE FROM submissions WHERE slug = ?" "$slug" || true
        echo "  Purged DB row"
        continue
    fi

    # Check if this is an orphan (no DB row at all)
    if ! echo "$ACTIVE_SLUGS" | grep -qx "$slug" && \
       ! echo "$DELETED_SLUGS" | grep -qx "$slug"; then
        echo "Cleaning orphaned staging dir: $slug"
        pgrep -f "download.sh.*$slug" | xargs -r kill 2>/dev/null || true
        rm -rf "$dir"
        echo "  Removed $dir"
    fi
done

echo "Cleanup complete."
