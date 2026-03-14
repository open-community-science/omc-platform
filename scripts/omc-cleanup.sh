#!/bin/bash
# OMC Cleanup Cron — removes staging files for soft-deleted submissions
# Run hourly: */60 * * * * /opt/omc-platform/scripts/omc-cleanup.sh
#
# Cleans up:
#   1. Staging dirs for deleted submissions (deleted_at > 1 hour ago)
#   2. Kills orphaned download processes for deleted submissions
#   3. Removes staging dirs with no matching DB submission (orphans)

set -euo pipefail

DB_PATH="${OMC_DB_PATH:-/opt/omc-platform/portal/omc.db}"
STAGING_DIR="${OMC_STAGING_DIR:-/data/sra_downloads}"
GRACE_PERIOD=3600  # seconds — don't clean up until 1 hour after delete

if [ ! -f "$DB_PATH" ]; then
    echo "DB not found: $DB_PATH"
    exit 1
fi

if [ ! -d "$STAGING_DIR" ]; then
    exit 0
fi

echo "=== OMC Cleanup: $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

# Find soft-deleted submissions older than grace period
CUTOFF=$(date -u -d "-${GRACE_PERIOD} seconds" +%Y-%m-%dT%H:%M:%S 2>/dev/null || \
         date -u -v-${GRACE_PERIOD}S +%Y-%m-%dT%H:%M:%S)

DELETED_SLUGS=$(sqlite3 "$DB_PATH" \
    "SELECT slug FROM submissions WHERE deleted_at IS NOT NULL AND deleted_at < '$CUTOFF';" 2>/dev/null || true)

# Also find staging dirs that have no matching submission at all (orphans)
ACTIVE_SLUGS=$(sqlite3 "$DB_PATH" \
    "SELECT slug FROM submissions WHERE deleted_at IS NULL;" 2>/dev/null || true)

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
        sqlite3 "$DB_PATH" "DELETE FROM submissions WHERE slug = '$slug';" 2>/dev/null || true
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
