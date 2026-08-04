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

# Which cluster is this? Alliance sets CC_CLUSTER (fir/nibi/…); fall back to host.
OMC_CLUSTER="${OMC_CLUSTER:-${CC_CLUSTER:-$(hostname -s 2>/dev/null || echo unknown)}}"

# Delete each Nextflow work dir once it has been archived to .sqsh. Work dirs are
# by far the largest consumer of the scratch file quota — a single amplicon run
# leaves ~70k inodes behind, so a batch of runs exhausts the 1M limit long before
# it runs out of bytes. The archive is written first and the delete only happens
# if mksquashfs succeeded, so this trades a re-extract step for the quota.
#
# Exported HERE and not in omc-pickup-loop.sh deliberately: the loop exports its
# environment once at job start and then runs for seven days, so a variable added
# there does nothing until the current loop job ends. This script is re-executed
# every 300s, so an edit takes effect on the next cycle.
export OMC_CLEANUP_WORKDIR="${OMC_CLEANUP_WORKDIR:-true}"

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

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# ── Cluster heartbeat + active-cluster gate ──────────────────────────────
# Tell arbutus we're alive (with our current job counts) and learn whether we
# are the *active* pickup cluster. Standby clusters still run Phase 0 below
# (reconciling their own in-flight jobs) but skip Phase 1/2 (new pickups), so
# multiple loops can run without racing for the same job. The failover switch
# lives in the admin panel (POST /admin/cluster/active).
#
# Fail OPEN: if the portal/endpoint is unreachable (old portal, network blip),
# IS_ACTIVE stays true and behaviour is unchanged — but /ready-runs would be
# empty in that case anyway, so nothing gets double-picked.
IS_ACTIVE=true
HB_RUN=$(squeue -h -u "$USER" -t RUNNING -o '%j' 2>/dev/null | grep -cvE '^omc-pickup')
HB_PEND=$(squeue -h -u "$USER" -t PENDING -o '%j' 2>/dev/null | grep -cvE '^omc-pickup')
HB_BODY=$(jq -nc --arg c "$OMC_CLUSTER" --arg h "$(hostname -s 2>/dev/null || echo '')" \
    --argjson r "${HB_RUN:-0}" --argjson p "${HB_PEND:-0}" --arg j "${SLURM_JOB_ID:-}" \
    '{cluster:$c,hostname:$h,running:$r,pending:$p,loop_job:$j}' 2>/dev/null)
HB_RESP=$(curl -sf --max-time 20 -X POST "${STAGING_API}/cluster/heartbeat" \
    -H "$AUTH_HEADER" -H "Content-Type: application/json" --data-raw "$HB_BODY" 2>/dev/null)
# NOTE: do NOT use jq's `.is_active // true` — `//` yields the RHS when the LHS is
# false *or* null, so a standby cluster (is_active=false) would wrongly read true.
# Only override the fail-open default when we got a well-formed response.
if [ -n "$HB_RESP" ]; then
    _act=$(echo "$HB_RESP" | jq -r 'if .is_active == true then "true" elif .is_active == false then "false" else "unknown" end' 2>/dev/null)
    [ "$_act" = "true" ] && IS_ACTIVE=true
    [ "$_act" = "false" ] && IS_ACTIVE=false
fi
echo "$(now) heartbeat ${OMC_CLUSTER}: active=${IS_ACTIVE} (running=$HB_RUN pending=$HB_PEND)"

# ── Phase 0: Reconcile submitted pipelines ───────────────────────────────
# The pipeline wrapper runs on a compute node and pushes status to arbutus, but
# those pushes don't reliably arrive (env propagation / node connectivity), which
# leaves submissions stuck showing QUEUED in the portal even after they finish.
# The pickup loop *does* reach arbutus, so relay the ground-truth markers the
# wrapper writes to shared scratch (.status/.completed/.transferred) from here —
# and upload the results archive if the wrapper couldn't.
#
# OOM recovery: if a pipeline was OOM-killed, resubmit it at the next memory
# tier (up to 3 attempts) by editing the sbatch --mem in its pipeline.sh, rather
# than reporting a hard failure. The assembly step caps its tools to OMC_MEM_GB,
# so a bigger allocation gives them a bigger real budget.
OMC_MEM_TIERS="128 187 249 373 498"
_oom_retry() {  # $1=slug $2=OUTPUT_DIR(with trailing /) $3=job_id ; 0 if resubmitted
    local slug="$1" out="$2" jid="$3" ps="${2}pipeline.sh"
    [ -f "$ps" ] || return 1
    # Was it OOM? Trust sacct state or the slurmstepd oom message in the logs.
    local oom=0 st=""
    [[ "$jid" =~ ^[0-9]+$ ]] && st=$(sacct -n -X -j "$jid" -o State 2>/dev/null | head -1 | tr -d ' ')
    [[ "$st" == OUT_OF_MEMORY* ]] && oom=1
    # Cover both native (std::bad_alloc) and JVM OOM — danaSeq's BBTools steps
    # (ERROR_CORRECT_ECCO, etc.) throw java.lang.OutOfMemoryError, which the old
    # pattern missed, so a heap OOM there failed hard instead of retrying bigger.
    grep -qiE 'oom[_-]kill|OOM Killed|Out of memory|std::bad_alloc|Cannot allocate memory|OutOfMemoryError|Java heap space|GC overhead limit' \
        /dev/null "${out}"slurm-pipeline-*.err "${out}"slurm-pipeline-*.out 2>/dev/null && oom=1
    [ "$oom" -eq 1 ] || return 1
    local att cur; att=$(sed -n 's/^OMC_MEM_ATTEMPT=//p' "$ps" | head -1); att=${att:-0}
    cur=$(sed -n 's/^OMC_MEM_GB=//p' "$ps" | head -1); cur=${cur:-128}
    local next=""; for t in $OMC_MEM_TIERS; do [ "$t" -gt "$cur" ] && { next=$t; break; }; done
    [ "$att" -ge 3 ] && return 1
    [ -z "$next" ] && return 1   # already at the top tier
    sed -i -E "s/^#SBATCH --mem=.*/#SBATCH --mem=${next}G/; \
               s/^OMC_MEM_GB=.*/OMC_MEM_GB=${next}/; \
               s/^OMC_MEM_ATTEMPT=.*/OMC_MEM_ATTEMPT=$((att+1))/" "$ps"
    # Fresh attempt: drop prior outputs + work dir, keep pipeline.sh and markers.
    find "$out" -mindepth 1 -maxdepth 1 \
        ! -name pipeline.sh ! -name job_ids.txt ! -name .pipeline-submitted \
        -exec rm -rf {} + 2>/dev/null
    rm -rf "${OMC_SCRATCH}/omc_work/${slug}" 2>/dev/null
    local newjob; newjob=$(cd /tmp && sbatch --parsable "$ps" 2>/dev/null)
    [[ "$newjob" =~ ^[0-9]+$ ]] || return 1
    echo "pipeline=$newjob" > "${out}job_ids.txt"
    push_status "$slug" "$(jq -nc --arg m "$next" \
        '{phase:"running",detail:("OOM — retrying at "+$m+"G")}')"
    echo "$(now) $slug OOM at ${cur}G -> resubmitted at ${next}G (attempt $((att+1))), job $newjob"
    return 0
}

shopt -s nullglob
for OUTPUT_DIR in "${OMC_RESULTS}"/*/; do
    [ -f "${OUTPUT_DIR}.pipeline-submitted" ] || continue
    [ -f "${OUTPUT_DIR}.finalized" ] && continue
    RSLUG=$(basename "$OUTPUT_DIR")
    LSTATUS=$(cat "${OUTPUT_DIR}.status" 2>/dev/null || echo "")
    JOB_ID=$(sed -n 's/^pipeline=//p' "${OUTPUT_DIR}job_ids.txt" 2>/dev/null | head -1)

    if [ ! -f "${OUTPUT_DIR}.completed" ]; then
        # Still running — or the node died before writing a completion marker.
        [ "$LSTATUS" = "running" ] && push_status "$RSLUG" '{"phase":"running","detail":"relayed by pickup"}'
        if [[ "$JOB_ID" =~ ^[0-9]+$ ]]; then
            JSTATE=$(sacct -j "$JOB_ID" -n -X -o State 2>/dev/null | head -1 | tr -d ' ')
            case "$JSTATE" in
                OUT_OF_MEMORY*)
                    _oom_retry "$RSLUG" "$OUTPUT_DIR" "$JOB_ID" && continue
                    push_status "$RSLUG" "$(jq -nc --arg j "$JOB_ID" \
                        '{phase:"failed",reason:("SLURM job "+$j+" OOM-killed (max retries reached)"),job_id:$j}')"
                    touch "${OUTPUT_DIR}.finalized"
                    echo "$(now) reconciled $RSLUG -> failed (OOM, retries exhausted)" ;;
                FAILED|CANCELLED*|TIMEOUT|NODE_FAIL|BOOT_FAIL|DEADLINE)
                    push_status "$RSLUG" "$(jq -nc --arg s "$JSTATE" --arg j "$JOB_ID" \
                        '{phase:"failed",reason:("SLURM job "+$j+" ended: "+$s+" with no completion marker"),job_id:$j}')"
                    touch "${OUTPUT_DIR}.finalized"
                    echo "$(now) reconciled $RSLUG -> failed (slurm $JSTATE)" ;;
            esac
        fi
        continue
    fi

    # Pipeline finished — relay the terminal outcome.
    EXIT=$(cat "${OUTPUT_DIR}.completed" 2>/dev/null || echo 1)
    if [ "$EXIT" = "0" ]; then
        # Success: make sure results reached arbutus (wrapper upload may have failed).
        if [ ! -f "${OUTPUT_DIR}.transferred" ] && [ -f "${OUTPUT_DIR%/}.sqsh" ]; then
            echo "$(now) $RSLUG completed but not transferred — uploading .sqsh from pickup"
            if curl -sf -X POST "${STAGING_API}/${RSLUG}/upload-results" \
                    -H "$AUTH_HEADER" -H "Content-Type: application/octet-stream" \
                    -T "${OUTPUT_DIR%/}.sqsh"; then
                touch "${OUTPUT_DIR}.transferred"
            else
                echo "$(now) WARN: pickup upload failed for $RSLUG — will retry next tick"
            fi
        fi
        if [ -f "${OUTPUT_DIR}.transferred" ]; then
            push_status "$RSLUG" '{"phase":"transferred","results_format":"archived"}'
            touch "${OUTPUT_DIR}.finalized"
            echo "$(now) reconciled $RSLUG -> transferred"
        else
            # No archive to upload yet (or upload failing) — report success, retry later.
            push_status "$RSLUG" '{"phase":"completed","exit_code":"0"}'
        fi
    else
        # A wrapper may report exit!=0 for an OOM-killed step — try to recover first.
        _oom_retry "$RSLUG" "$OUTPUT_DIR" "$JOB_ID" && continue
        # Extract a MEANINGFUL failure reason. Nextflow animates progress lines
        # like "[-  ] ERROR_CORRECT_ECCO -" whose PROCESS NAME contains "ERROR",
        # so a naive grep for ERROR grabbed the progress bar instead of the real
        # error. Prefer, in order: the failed-process line, the actual stderr in
        # the "Command error:" block, then a generic error line — always skipping
        # Nextflow's own bracketed progress/status lines.
        LOGS=$(printf '%s ' /dev/null "${OUTPUT_DIR}"slurm-pipeline-*.err "${OUTPUT_DIR}"slurm-pipeline-*.out)
        _grep() { grep -hE "$1" $LOGS 2>/dev/null | grep -vE '^\[[-= ]*\]|process > .*[0-9]+%' | head -1 | cut -c1-300; }
        # 1) which process failed + exit status
        REASON=$(_grep 'Error executing process|terminated with an error exit status|Caused by:')
        # 2) the first real stderr line after "Command error:"
        if [ -z "$REASON" ]; then
            REASON=$(grep -hA3 'Command error:' $LOGS 2>/dev/null \
                | grep -vE 'Command error:|^--|^\[[-= ]*\]' | grep -vE '^\s*$' | head -1 | cut -c1-300)
        fi
        # 3) generic fallback (still excluding progress bars)
        [ -n "$REASON" ] || REASON=$(_grep 'No such variable|Exception|Traceback|Killed|OOM|OutOfMemoryError|Java heap space|std::bad_alloc|Cannot allocate')
        [ -n "$REASON" ] || REASON="Pipeline exited with code $EXIT"
        push_status "$RSLUG" "$(jq -nc --arg r "$REASON" --arg e "$EXIT" --arg j "$JOB_ID" \
            '{phase:"failed",reason:$r,exit_code:$e,job_id:$j}')"
        touch "${OUTPUT_DIR}.finalized"
        echo "$(now) reconciled $RSLUG -> failed (exit $EXIT)"
    fi
done
shopt -u nullglob

# ── Phase 1: Pick up individual runs as they complete ────────────────────
# Only the active cluster picks up NEW jobs. Standby clusters stop here (Phase 0
# reconciliation above already ran for their own in-flight jobs).
if [ "$IS_ACTIVE" != "true" ]; then
    echo "$(now) standby (${OMC_CLUSTER}) — skipping new pickups"
    exit 0
fi

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

        # Parse only the "Submitted batch job N" line — sbatch also emits a
        # memory-unit NOTE ending in "1000M." that awk '{print $NF}' would grab.
        PIPELINE_JOB_ID=$(echo "$SBATCH_OUT" | awk '/Submitted batch job/{print $NF; exit}')
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
