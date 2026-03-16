#!/bin/bash
# omc-pickup.sh — Runs on fir via cron. Polls arbutus staging API for
# ready SRA runs, fetches files incrementally as they complete, and
# submits pipeline jobs once all runs are downloaded.
# Pushes status updates back to arbutus so the portal never needs SSH.
#
# Configuration via environment variables (set in crontab or wrapper):
#   OMC_STAGING_URL    - Base URL of staging API (e.g. https://microbial.opencommunity.science)
#   OMC_STAGING_KEY    - Bearer token for staging API auth
#   OMC_SCRATCH        - HPC scratch directory (e.g. /home/user/scratch)
#   OMC_RESULTS        - Results directory (e.g. /home/user/scratch/omc_results)
#
# Crontab example (every 2 minutes):
#   */2 * * * * OMC_STAGING_URL=https://microbial.opencommunity.science OMC_STAGING_KEY=xxx OMC_SCRATCH=$HOME/scratch OMC_RESULTS=$HOME/scratch/omc_results /path/to/omc-pickup.sh >> /tmp/omc-pickup.log 2>&1

set -uo pipefail

# Required env vars
: "${OMC_STAGING_URL:?Set OMC_STAGING_URL (e.g. https://microbial.opencommunity.science)}"
: "${OMC_STAGING_KEY:?Set OMC_STAGING_KEY (staging API bearer token)}"
: "${OMC_SCRATCH:?Set OMC_SCRATCH (e.g. \$HOME/scratch)}"
: "${OMC_RESULTS:?Set OMC_RESULTS (e.g. \$HOME/scratch/omc_results)}"

AUTH_HEADER="Authorization: Bearer ${OMC_STAGING_KEY}"
STAGING_API="${OMC_STAGING_URL}/staging"

# Push a status update back to arbutus
push_status() {
    local slug="$1"
    local json="$2"
    curl -sf -X POST "${STAGING_API}/${slug}/status" \
        -H "$AUTH_HEADER" \
        -H "Content-Type: application/json" \
        --data-raw "$json" >/dev/null 2>&1 || true
}

# Lock file to prevent overlapping runs
LOCKFILE="${OMC_SCRATCH}/.omc-pickup.lock"
exec 200>"$LOCKFILE"
flock -n 200 || { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) Another pickup is already running"; exit 0; }

# ── Phase 1: Pick up individual runs as they complete ────────────────────

READY_JSON=$(curl -sf -H "$AUTH_HEADER" "${STAGING_API}/ready-runs" 2>/dev/null)
if [ $? -ne 0 ] || [ -z "$READY_JSON" ]; then
    # Fall through to legacy check
    READY_JSON='{"ready_runs":[]}'
fi

# Process each slug's ready runs
echo "$READY_JSON" | jq -c '.ready_runs[]' 2>/dev/null | while IFS= read -r slug_entry; do
    SLUG=$(echo "$slug_entry" | jq -r '.slug')
    ALL_DONE=$(echo "$slug_entry" | jq -r '.all_done')
    HAS_PIPELINE=$(echo "$slug_entry" | jq -r '.has_pipeline')

    INPUT_DIR="${OMC_SCRATCH}/sra_downloads/${SLUG}"
    OUTPUT_DIR="${OMC_RESULTS}/${SLUG}"
    mkdir -p "${INPUT_DIR}/fastq" "${OUTPUT_DIR}"

    # Download each ready run
    echo "$slug_entry" | jq -c '.runs[]' | while IFS= read -r run_entry; do
        ACC=$(echo "$run_entry" | jq -r '.accession')

        # Skip if we already have this run
        if [ -f "${INPUT_DIR}/.fetched-${ACC}" ]; then
            continue
        fi

        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) Fetching run $ACC for $SLUG"
        push_status "$SLUG" "{\"phase\":\"downloading\",\"detail\":\"Transferring $ACC to HPC\"}"

        # Download each file for this run (one at a time, with retry)
        DOWNLOAD_OK=true
        for FNAME in $(echo "$run_entry" | jq -r '.files[]'); do
            echo "  Downloading $FNAME..."
            DL_OK=false
            for attempt in 1 2 3; do
                curl -f --max-time 7200 --retry 2 --retry-delay 10 \
                    -H "$AUTH_HEADER" \
                    "${STAGING_API}/${SLUG}/download/fastq/${FNAME}" \
                    -o "${INPUT_DIR}/fastq/${FNAME}" 2>/dev/null
                if [ $? -eq 0 ] && [ -s "${INPUT_DIR}/fastq/${FNAME}" ]; then
                    DL_OK=true
                    echo "  Downloaded: $(du -h "${INPUT_DIR}/fastq/${FNAME}" | cut -f1)"
                    break
                fi
                echo "  Attempt $attempt failed, retrying in 30s..."
                rm -f "${INPUT_DIR}/fastq/${FNAME}"
                sleep 30
            done
            if [ "$DL_OK" != "true" ]; then
                echo "  ERROR: Failed to download $FNAME after 3 attempts"
                rm -f "${INPUT_DIR}/fastq/${FNAME}"
                DOWNLOAD_OK=false
            fi
        done

        if [ "$DOWNLOAD_OK" = "true" ]; then
            # Mark locally as fetched
            date -u +%Y-%m-%dT%H:%M:%SZ > "${INPUT_DIR}/.fetched-${ACC}"

            # Tell arbutus to delete this run's files (free disk on arbutus)
            curl -sf -X POST -H "$AUTH_HEADER" \
                "${STAGING_API}/${SLUG}/run-picked-up/${ACC}" >/dev/null
            echo "  Run $ACC transferred and confirmed"
        else
            echo "  ERROR: Failed to fetch run $ACC"
        fi
    done

    # ── Phase 2: If all runs done, fetch pipeline.sh and submit ──────────

    if [ "$ALL_DONE" = "true" ] && [ "$HAS_PIPELINE" = "true" ]; then
        # Check if pipeline already submitted
        if [ -f "${OUTPUT_DIR}/.pipeline-submitted" ]; then
            continue
        fi

        # Download pipeline.sh
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) All runs done for $SLUG — fetching pipeline.sh"
        curl -f --max-time 60 --retry 3 --retry-delay 10 \
            -H "$AUTH_HEADER" "${STAGING_API}/${SLUG}/download/pipeline.sh" \
            -o "${OUTPUT_DIR}/pipeline.sh"
        if [ $? -ne 0 ]; then
            echo "  ERROR: Failed to download pipeline.sh"
            push_status "$SLUG" '{"phase":"failed","reason":"Failed to download pipeline.sh"}'
            continue
        fi

        # Verify we have fastq files
        NUM_FILES=$(ls "${INPUT_DIR}/fastq/"*.fastq* 2>/dev/null | wc -l)
        if [ "$NUM_FILES" -eq 0 ]; then
            echo "  ERROR: No fastq files on HPC for $SLUG"
            push_status "$SLUG" '{"phase":"failed","reason":"No fastq files after transfer"}'
            continue
        fi
        echo "  Have $NUM_FILES fastq files — submitting pipeline"

        # Submit pipeline
        push_status "$SLUG" '{"phase":"downloading","detail":"Submitting pipeline"}'
        SBATCH_OUT=$(sbatch --export=ALL,OMC_STAGING_URL="${OMC_STAGING_URL}",OMC_STAGING_KEY="${OMC_STAGING_KEY}" \
            "${OUTPUT_DIR}/pipeline.sh" 2>&1)
        SBATCH_RC=$?
        if [ $SBATCH_RC -ne 0 ]; then
            echo "  ERROR: sbatch failed: $SBATCH_OUT"
            push_status "$SLUG" "{\"phase\":\"failed\",\"reason\":\"sbatch failed: ${SBATCH_OUT}\"}"
            continue
        fi

        PIPELINE_JOB_ID=$(echo "$SBATCH_OUT" | awk '{print $NF}')
        echo "pipeline=${PIPELINE_JOB_ID}" > "${OUTPUT_DIR}/job_ids.txt"
        echo "pipeline_queued" > "${OUTPUT_DIR}/.status"
        date -u +%Y-%m-%dT%H:%M:%SZ > "${OUTPUT_DIR}/.pipeline-submitted"
        echo "  Pipeline submitted: job $PIPELINE_JOB_ID"

        push_status "$SLUG" "{\"phase\":\"queued\",\"job_id\":\"${PIPELINE_JOB_ID}\"}"

        # Clean up arbutus staging entirely
        curl -sf -X POST -H "$AUTH_HEADER" "${STAGING_API}/${SLUG}/picked-up" >/dev/null
        echo "  Staging cleaned up on arbutus"

        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) Done with $SLUG"
    fi
done
