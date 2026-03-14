#!/bin/bash
# OMC Download Worker — processes queued SRA downloads with concurrency control
#
# Runs as a systemd service. Watches the staging directory for .queued markers,
# picks them up FIFO, and runs up to MAX_JOBS concurrent downloads.
#
# Config via environment:
#   OMC_STAGING_DIR    — staging directory (default: /data/sra_downloads)
#   OMC_MAX_JOBS       — max concurrent downloads (default: 3)
#   OMC_POLL_INTERVAL  — seconds between queue checks (default: 10)

set -o pipefail

STAGING_DIR="${OMC_STAGING_DIR:-/data/sra_downloads}"
MAX_JOBS="${OMC_MAX_JOBS:-3}"
POLL_INTERVAL="${OMC_POLL_INTERVAL:-10}"

echo "=== OMC Download Worker ==="
echo "Staging dir: $STAGING_DIR"
echo "Max concurrent: $MAX_JOBS"
echo "Poll interval: ${POLL_INTERVAL}s"

# Track active jobs: ACTIVE_PIDS[slug]=pid
declare -A ACTIVE_PIDS

reap_finished() {
    # Check each tracked PID — remove if no longer running
    for slug in "${!ACTIVE_PIDS[@]}"; do
        pid="${ACTIVE_PIDS[$slug]}"
        if ! kill -0 "$pid" 2>/dev/null; then
            # Process finished — check exit code
            wait "$pid" 2>/dev/null
            exit_code=$?
            if [ $exit_code -eq 0 ]; then
                echo "[$(date -u +%H:%M:%S)] Download complete: $slug"
            else
                echo "[$(date -u +%H:%M:%S)] Download FAILED: $slug (exit $exit_code)"
            fi
            unset "ACTIVE_PIDS[$slug]"
        fi
    done
}

while true; do
    # Reap any finished jobs first
    reap_finished

    running=${#ACTIVE_PIDS[@]}

    # Find all .queued markers, sorted by timestamp (oldest first)
    queued=()
    for marker in "$STAGING_DIR"/*/.queued; do
        [ -f "$marker" ] || continue
        queued+=("$marker")
    done

    if [ ${#queued[@]} -gt 0 ]; then
        IFS=$'\n' sorted=($(ls -t -r "${queued[@]}" 2>/dev/null)); unset IFS

        for marker in "${sorted[@]}"; do
            dir=$(dirname "$marker")
            slug=$(basename "$dir")

            # Already running this slug
            [ -n "${ACTIVE_PIDS[$slug]:-}" ] && continue

            # Check concurrency limit
            if [ ${#ACTIVE_PIDS[@]} -ge "$MAX_JOBS" ]; then
                echo "[$(date -u +%H:%M:%S)] $slug: waiting (${#ACTIVE_PIDS[@]}/${MAX_JOBS} slots in use)"
                break
            fi

            echo "[$(date -u +%H:%M:%S)] Starting download: $slug"

            # Remove .queued marker and launch
            rm -f "$marker"
            bash "$dir/download.sh" > "$dir/download.log" 2>&1 &
            ACTIVE_PIDS[$slug]=$!
        done
    fi

    sleep "$POLL_INTERVAL"
done
