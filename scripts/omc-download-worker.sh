#!/bin/bash
# OMC Download Worker — processes queued SRA downloads with concurrency control
#
# Runs as a systemd service. Watches the staging directory for .queued markers,
# picks them up one at a time, runs the download, then moves to the next.
#
# Config via environment:
#   OMC_STAGING_DIR  — staging directory (default: /data/sra_downloads)
#   OMC_MAX_JOBS     — max concurrent downloads (default: 2)
#   OMC_POLL_INTERVAL — seconds between queue checks (default: 10)

set -uo pipefail

STAGING_DIR="${OMC_STAGING_DIR:-/data/sra_downloads}"
MAX_JOBS="${OMC_MAX_JOBS:-2}"
POLL_INTERVAL="${OMC_POLL_INTERVAL:-10}"

echo "=== OMC Download Worker ==="
echo "Staging dir: $STAGING_DIR"
echo "Max concurrent: $MAX_JOBS"
echo "Poll interval: ${POLL_INTERVAL}s"

active_jobs() {
    # Count currently running download.sh processes we started
    jobs -r 2>/dev/null | wc -l
}

while true; do
    # Find all .queued markers, sorted by timestamp (oldest first)
    queued=()
    for marker in "$STAGING_DIR"/*/.queued; do
        [ -f "$marker" ] || continue
        queued+=("$marker")
    done

    if [ ${#queued[@]} -gt 0 ]; then
        # Sort by file modification time (oldest first)
        IFS=$'\n' sorted=($(ls -t -r "${queued[@]}" 2>/dev/null)); unset IFS

        for marker in "${sorted[@]}"; do
            dir=$(dirname "$marker")
            slug=$(basename "$dir")

            # Check concurrency limit
            running=$(jobs -rp | wc -l)
            if [ "$running" -ge "$MAX_JOBS" ]; then
                echo "[$(date -u +%H:%M:%S)] $slug: waiting (${running}/${MAX_JOBS} slots in use)"
                break  # wait for next poll cycle
            fi

            # Skip if already running or completed
            [ -f "$dir/.status" ] && continue
            [ -f "$dir/.ready" ] && continue

            echo "[$(date -u +%H:%M:%S)] Starting download: $slug"

            # Remove .queued marker and launch
            rm -f "$marker"
            (
                bash "$dir/download.sh" > "$dir/download.log" 2>&1
                exit_code=$?
                if [ $exit_code -ne 0 ]; then
                    echo "[$(date -u +%H:%M:%S)] Download FAILED: $slug (exit $exit_code)"
                else
                    echo "[$(date -u +%H:%M:%S)] Download complete: $slug"
                fi
            ) &

        done
    fi

    # Reap finished background jobs
    wait -n 2>/dev/null || true

    sleep "$POLL_INTERVAL"
done
