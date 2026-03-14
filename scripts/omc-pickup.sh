#!/bin/bash
# omc-pickup.sh — Runs on fir via cron. Polls arbutus staging API for
# ready SRA downloads, fetches files over HTTP, and submits pipeline jobs.
# Pushes status updates back to arbutus so the portal never needs SSH.
#
# Configuration via environment variables (set in crontab or wrapper):
#   OMC_STAGING_URL    - Base URL of staging API (e.g. https://microbial.opencommunity.science)
#   OMC_STAGING_KEY    - Bearer token for staging API auth
#   OMC_SCRATCH        - HPC scratch directory (e.g. /home/user/scratch)
#   OMC_RESULTS        - Results directory (e.g. /home/user/scratch/omc_results)
#
# Crontab example:
#   */5 * * * * OMC_STAGING_URL=https://microbial.opencommunity.science OMC_STAGING_KEY=xxx OMC_SCRATCH=$HOME/scratch OMC_RESULTS=$HOME/scratch/omc_results /path/to/omc-pickup.sh >> /tmp/omc-pickup.log 2>&1

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

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) Checking for ready downloads..."

# Poll for ready slugs
READY_JSON=$(curl -sf -H "$AUTH_HEADER" "${STAGING_API}/ready" 2>/dev/null)
if [ $? -ne 0 ] || [ -z "$READY_JSON" ]; then
    echo "  No response from staging API (or empty)"
    exit 0
fi

# Parse ready slugs (requires jq)
SLUGS=$(echo "$READY_JSON" | jq -r '.ready[].slug' 2>/dev/null)
if [ -z "$SLUGS" ]; then
    echo "  No ready downloads"
    exit 0
fi

for SLUG in $SLUGS; do
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) Processing: $SLUG"

    INPUT_DIR="${OMC_SCRATCH}/sra_downloads/${SLUG}"
    OUTPUT_DIR="${OMC_RESULTS}/${SLUG}"
    mkdir -p "${INPUT_DIR}/fastq" "${OUTPUT_DIR}"

    # Tell arbutus we're transferring
    push_status "$SLUG" '{"phase":"downloading","detail":"Transferring to HPC"}'

    # Get file list
    FILES_JSON=$(curl -sf -H "$AUTH_HEADER" "${STAGING_API}/${SLUG}/files")
    if [ $? -ne 0 ]; then
        echo "  ERROR: Failed to list files for $SLUG"
        push_status "$SLUG" '{"phase":"failed","reason":"Failed to list staging files"}'
        continue
    fi

    # Download pipeline.sh
    echo "  Downloading pipeline.sh..."
    curl -sf -H "$AUTH_HEADER" "${STAGING_API}/${SLUG}/download/pipeline.sh" \
        -o "${OUTPUT_DIR}/pipeline.sh"
    if [ $? -ne 0 ]; then
        echo "  ERROR: Failed to download pipeline.sh"
        push_status "$SLUG" '{"phase":"failed","reason":"Failed to download pipeline.sh"}'
        continue
    fi

    # Download all fastq files
    FASTQ_FILES=$(echo "$FILES_JSON" | jq -r '.files[] | select(.path | startswith("fastq/")) | .path')
    DOWNLOAD_OK=true
    for FPATH in $FASTQ_FILES; do
        FNAME=$(basename "$FPATH")
        echo "  Downloading $FNAME..."
        curl -sf -H "$AUTH_HEADER" "${STAGING_API}/${SLUG}/download/${FPATH}" \
            -o "${INPUT_DIR}/fastq/${FNAME}"
        if [ $? -ne 0 ]; then
            echo "  ERROR: Failed to download $FNAME"
            DOWNLOAD_OK=false
        fi
    done

    if [ "$DOWNLOAD_OK" != "true" ]; then
        echo "  Some files failed to download — aborting"
        push_status "$SLUG" '{"phase":"failed","reason":"File transfer incomplete"}'
        continue
    fi

    # Verify we got files
    NUM_FILES=$(ls "${INPUT_DIR}/fastq/"*.fastq* 2>/dev/null | wc -l)
    if [ "$NUM_FILES" -eq 0 ]; then
        echo "  ERROR: No fastq files after download"
        push_status "$SLUG" '{"phase":"failed","reason":"No fastq files after transfer"}'
        continue
    fi
    echo "  Downloaded $NUM_FILES fastq files"

    # Submit pipeline — export env vars so pipeline.sh can push status too
    echo "  Submitting pipeline job..."
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
    echo "  Pipeline submitted: job $PIPELINE_JOB_ID"

    # Push queued status with job ID
    push_status "$SLUG" "{\"phase\":\"queued\",\"job_id\":\"${PIPELINE_JOB_ID}\"}"

    # Tell arbutus to clean up staging
    curl -sf -X POST -H "$AUTH_HEADER" "${STAGING_API}/${SLUG}/picked-up" >/dev/null
    echo "  Notified arbutus — staging cleaned up"

    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) Done with $SLUG"
done
