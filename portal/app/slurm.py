"""SLURM job submission and monitoring — SSH-free.

Job flow:
  1. Portal downloads SRA data locally on arbutus, stages for HTTP pickup
  2. Cron on fir polls /staging/ready, downloads files, submits sbatch
  3. Fir pushes status updates back to /staging/{slug}/status over HTTP
  4. Portal reads local staging markers + pushed HPC status (no SSH)
"""
import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path

from .config import get_settings
from .database import Submission, PipelineType

settings = get_settings()
logger = logging.getLogger(__name__)


def _get_pipeline_path(pipeline: PipelineType) -> str:
    """Return the HPC path for a given pipeline type."""
    pipeline_paths = {
        PipelineType.NANOPORE_MAG: settings.pipeline_nanopore_mag,
        PipelineType.MICROSCAPE: settings.pipeline_microscape,
        PipelineType.ILLUMINA_MAG: settings.pipeline_illumina_mag,
        PipelineType.RNASEQ: settings.pipeline_rnaseq,
        PipelineType.ISOLATE_GENOME: settings.pipeline_isolate_genome,
    }

    path = pipeline_paths.get(pipeline)
    if not path:
        raise NotImplementedError(
            f"Pipeline '{pipeline.value}' is not yet available for HPC submission."
        )
    return path


def _build_pipeline_script(submission: Submission) -> str:
    """Generate sbatch script for pipeline execution (heavy job).

    The pipeline script also pushes status updates back to arbutus
    via the staging API so the portal can track progress without SSH.
    """
    pipeline_path = _get_pipeline_path(submission.pipeline)
    scratch = settings.hpc_scratch
    db_dir = settings.hpc_db_dir
    output_dir = f"{settings.results_path}/{submission.slug}"
    input_dir = f"{scratch}/sra_downloads/{submission.slug}"
    work_dir = f"{scratch}/omc_work/{submission.slug}"
    accession = submission.bioproject_accession

    # Pipeline-specific run command
    if submission.pipeline == PipelineType.NANOPORE_MAG:
        pipeline_cmd = f"""{pipeline_path}/run-mag.sh --apptainer \\
    --all \\
    --run_sendsketch false \\
    --run_vamb_tax false \\
    --input ${{INPUT_DIR}}/fastq \\
    --outdir ${{OUTPUT_DIR}} \\
    --workdir ${{WORK_DIR}} \\
    --db_dir {db_dir} \\
    --assembly_memory '100 GB' \\
    --assembly_cpus 32"""
    elif submission.pipeline == PipelineType.MICROSCAPE:
        pipeline_cmd = f"""cd {pipeline_path}
nextflow run main.nf \\
    --input ${{INPUT_DIR}}/fastq \\
    --outdir ${{OUTPUT_DIR}} \\
    -w ${{WORK_DIR}} \\
    -profile apptainer"""
    else:
        pipeline_cmd = f"""echo "Pipeline {submission.pipeline.value} not yet implemented"
exit 1"""

    return f"""#!/bin/bash
#SBATCH --job-name=omc-run-{submission.slug}
#SBATCH --account={settings.slurm_account}
#SBATCH --time=24:00:00
#SBATCH --cpus-per-task=32
#SBATCH --mem=128G
#SBATCH --output={output_dir}/slurm-pipeline-%j.out
#SBATCH --error={output_dir}/slurm-pipeline-%j.err

set -uo pipefail

INPUT_DIR="{input_dir}"
OUTPUT_DIR="{output_dir}"
WORK_DIR="{work_dir}"

# Status reporting — push updates to arbutus over HTTP
# These env vars are set by omc-pickup.sh before sbatch
OMC_STAGING_URL="${{OMC_STAGING_URL:-}}"
OMC_STAGING_KEY="${{OMC_STAGING_KEY:-}}"
SLUG="{submission.slug}"

push_status() {{
    local phase="$1"
    local extra="${{2:-}}"
    if [ -n "$OMC_STAGING_URL" ] && [ -n "$OMC_STAGING_KEY" ]; then
        curl -sf -X POST "${{OMC_STAGING_URL}}/staging/${{SLUG}}/status" \\
            -H "Authorization: Bearer ${{OMC_STAGING_KEY}}" \\
            -H "Content-Type: application/json" \\
            --data-raw "{{\\"phase\\":\\"$phase\\"$extra}}" >/dev/null 2>&1 || true
    fi
}}

# Trap SLURM signals (timeout/cancel) to write status markers
cleanup() {{
    echo "Signal caught — marking job as failed"
    echo "failed" > ${{OUTPUT_DIR}}/.status
    echo 1 > ${{OUTPUT_DIR}}/.completed
    push_status "failed" ',\\"reason\\":\\"Signal caught\\"'
    exit 1
}}
trap cleanup SIGTERM SIGUSR1 SIGUSR2

# Load modules
module load apptainer 2>/dev/null || true
export APPTAINER_CACHEDIR={scratch}/apptainer_cache
export APPTAINER_TMPDIR=${{SLURM_TMPDIR:-/tmp}}

# Create directories
mkdir -p "${{OUTPUT_DIR}}" "${{WORK_DIR}}"

# Verify download data exists
NUM_FILES=$(ls ${{INPUT_DIR}}/fastq/*.fastq* 2>/dev/null | wc -l)
if [ "$NUM_FILES" -eq 0 ]; then
    echo "ERROR: No fastq files found in ${{INPUT_DIR}}/fastq/"
    echo "failed" > ${{OUTPUT_DIR}}/.status
    echo 1 > ${{OUTPUT_DIR}}/.completed
    push_status "failed" ',\\"reason\\":\\"No fastq files\\"'
    exit 1
fi

echo "running" > ${{OUTPUT_DIR}}/.status
push_status "running" ',\\"job_id\\":\\"'$SLURM_JOB_ID'\\",\\"slurm_state\\":\\"RUNNING\\"'

echo "=== OMC Pipeline: {submission.pipeline.value} (--all) ==="
echo "Accession: {accession}"
echo "Slug: {submission.slug}"
echo "Input files: $NUM_FILES"
echo "Started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Capture exit code — don't let set -e kill the script before writing markers
{pipeline_cmd} && PIPELINE_EXIT=0 || PIPELINE_EXIT=$?

echo "--- Pipeline finished with exit code $PIPELINE_EXIT ---"
echo "Completed: $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Signal completion
echo $PIPELINE_EXIT > ${{OUTPUT_DIR}}/.completed
if [ $PIPELINE_EXIT -eq 0 ]; then
    echo "completed" > ${{OUTPUT_DIR}}/.status
    push_status "completed" ',\\"exit_code\\":\\"0\\"'
else
    echo "failed" > ${{OUTPUT_DIR}}/.status
    push_status "failed" ',\\"exit_code\\":\\"'$PIPELINE_EXIT'\\",\\"reason\\":\\"Pipeline exited with code '$PIPELINE_EXIT'\\"'
fi
"""


def _build_local_download_wrapper(submission: Submission) -> str:
    """Generate a wrapper script that runs locally on arbutus.

    Downloads SRA data to local staging, writes .ready marker.
    A cron job on fir polls the staging API over HTTP to pick up files
    and submit the pipeline — no SSH required.
    """
    local_dir = f"{settings.local_download_path}/{submission.slug}"
    accession = submission.bioproject_accession

    # Extract run accessions from selected_runs if available
    run_accessions = []
    if submission.selected_runs:
        for row in submission.selected_runs:
            if isinstance(row, dict) and row.get("run_accessions"):
                run_accessions.extend(row["run_accessions"])

    # Helper function: prefetch with retries + backoff, then fasterq-dump
    download_fn = f"""
check_disk() {{
    # Returns available GB on the staging volume
    local avail_kb=$(df --output=avail "${{LOCAL_DIR}}" 2>/dev/null | tail -1)
    echo $(( ${{avail_kb:-0}} / 1048576 ))
}}

download_run() {{
    local acc="$1"
    local STALL_TIMEOUT=600  # kill if no new bytes for 10 min
    local STALL_CHECK=30     # check every 30s
    local MAX_RETRIES=3
    local RETRY_DELAY=30     # seconds between retries
    local MIN_DISK_GB=50     # pause if less than 50GB free

    # Check disk before starting this run
    local avail=$(check_disk)
    if [ "$avail" -lt "$MIN_DISK_GB" ]; then
        echo "  SKIPPED: $acc — only ${{avail}}GB free (need ${{MIN_DISK_GB}}GB)" | tee -a ${{LOCAL_DIR}}/failed_runs.txt
        return 1
    fi

    echo "Downloading $acc... (${{avail}}GB free)"
    local attempt=1
    while [ $attempt -le $MAX_RETRIES ]; do
        # Run prefetch in background with stall detection
        prefetch "$acc" -O ${{LOCAL_DIR}} &
        local pid=$!
        local last_size=0
        local stall_seconds=0

        while kill -0 $pid 2>/dev/null; do
            sleep $STALL_CHECK
            local cur_size=$(du -sb "${{LOCAL_DIR}}/${{acc}}" 2>/dev/null | cut -f1)
            cur_size=${{cur_size:-0}}
            if [ "$cur_size" -eq "$last_size" ]; then
                stall_seconds=$((stall_seconds + STALL_CHECK))
                if [ $stall_seconds -ge $STALL_TIMEOUT ]; then
                    echo "  prefetch stalled for $acc (no new bytes for ${{STALL_TIMEOUT}}s) — killing"
                    kill $pid 2>/dev/null; wait $pid 2>/dev/null
                    break
                fi
            else
                stall_seconds=0
                last_size=$cur_size
            fi
        done

        # Check if prefetch succeeded
        wait $pid 2>/dev/null
        local exit_code=$?
        if [ $exit_code -eq 0 ]; then
            echo "  prefetch OK for $acc (attempt $attempt)"
            break
        fi

        echo "  prefetch failed (attempt $attempt/$MAX_RETRIES, exit $exit_code)"
        rm -rf "${{LOCAL_DIR}}/${{acc}}" 2>/dev/null
        if [ $attempt -lt $MAX_RETRIES ]; then
            echo "  waiting ${{RETRY_DELAY}}s before retry..."
            sleep $RETRY_DELAY
            RETRY_DELAY=$((RETRY_DELAY * 2))
        fi
        attempt=$((attempt + 1))
    done

    if [ $attempt -gt $MAX_RETRIES ]; then
        echo "  FAILED: $acc (gave up after $MAX_RETRIES attempts)" | tee -a ${{LOCAL_DIR}}/failed_runs.txt
        return 1
    fi

    fasterq-dump "${{LOCAL_DIR}}/${{acc}}/${{acc}}.sra" -O ${{LOCAL_DIR}}/fastq -t ${{LOCAL_DIR}}/tmp -e 2 && \\
    pigz -p 2 ${{LOCAL_DIR}}/fastq/${{acc}}*.fastq
    if [ $? -ne 0 ]; then
        echo "  FAILED: fasterq-dump/pigz for $acc" | tee -a ${{LOCAL_DIR}}/failed_runs.txt
        return 1
    fi
    # Clean up .sra file to save disk (fastq.gz is what we need)
    rm -rf "${{LOCAL_DIR}}/${{acc}}"
    # Mark this run as ready for pickup by fir
    mkdir -p "${{LOCAL_DIR}}/.run-ready"
    date -u +%Y-%m-%dT%H:%M:%SZ > "${{LOCAL_DIR}}/.run-ready/${{acc}}"
    echo "  Run $acc staged for pickup"
    return 0
}}
export -f check_disk download_run
export LOCAL_DIR

PARALLEL_JOBS=4  # concurrent downloads per job (network-bound)
"""

    if run_accessions:
        # Write accessions to a file and process in parallel
        acc_lines = "\n".join(run_accessions)
        download_cmd = download_fn + f"""
cat > ${{LOCAL_DIR}}/run_list.txt << 'ACCLIST'
{acc_lines}
ACCLIST
echo "Downloading $(wc -l < ${{LOCAL_DIR}}/run_list.txt) runs ($PARALLEL_JOBS in parallel)..."
cat ${{LOCAL_DIR}}/run_list.txt | xargs -P $PARALLEL_JOBS -I {{}} bash -c 'download_run "$@" || true' _ {{}}
"""
    else:
        logger.warning(f"No run_accessions in selected_runs for {submission.slug} — downloading all runs (local mode)")
        download_cmd = f"""{download_fn}
echo "WARNING: No explicit run accessions — downloading ALL runs for {accession}"
echo "Resolving runs for {accession}..."
esearch -db sra -query "{accession}" | efetch -format runinfo | \\
    awk -F',' 'NR>1 && $1 != "" {{print $1}}' > ${{LOCAL_DIR}}/run_list.txt

NUM_RUNS=$(wc -l < ${{LOCAL_DIR}}/run_list.txt)
echo "Found $NUM_RUNS runs to download ($PARALLEL_JOBS in parallel)"

cat ${{LOCAL_DIR}}/run_list.txt | xargs -P $PARALLEL_JOBS -I {{}} bash -c 'download_run "$@" || true' _ {{}}
"""

    return f"""#!/bin/bash
# OMC Local Download Wrapper — runs on arbutus, stages for HTTP pickup by fir
set -uo pipefail

LOCAL_DIR="{local_dir}"

# Trap: if the script dies, write a failure marker
cleanup_on_failure() {{
    local exit_code=$?
    if [ $exit_code -ne 0 ]; then
        echo "Local download wrapper died with exit code $exit_code at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "failed" > ${{LOCAL_DIR}}/.status
    fi
}}
trap cleanup_on_failure EXIT

mkdir -p "${{LOCAL_DIR}}/fastq" "${{LOCAL_DIR}}/tmp"

echo "=== OMC Local Download: {accession} ==="
echo "Slug: {submission.slug}"
echo "Started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Pre-flight disk check
AVAIL_GB=$(df --output=avail "${{LOCAL_DIR}}" 2>/dev/null | tail -1)
AVAIL_GB=$(( ${{AVAIL_GB:-0}} / 1048576 ))
echo "Disk available: ${{AVAIL_GB}}GB"
if [ "$AVAIL_GB" -lt 50 ]; then
    echo "ERROR: Not enough disk space (${{AVAIL_GB}}GB free, need at least 50GB)"
    echo "failed" > ${{LOCAL_DIR}}/.status
    exit 1
fi

echo "downloading" > ${{LOCAL_DIR}}/.status

{download_cmd}

echo "Download complete. Files:"
ls -lh ${{LOCAL_DIR}}/fastq/
NUM_FILES=$(ls ${{LOCAL_DIR}}/fastq/*.fastq* 2>/dev/null | wc -l)
FAILED_RUNS=0
if [ -f "${{LOCAL_DIR}}/failed_runs.txt" ]; then
    FAILED_RUNS=$(wc -l < ${{LOCAL_DIR}}/failed_runs.txt)
fi
echo "Total: $NUM_FILES fastq files ($FAILED_RUNS failed)"

if [ "$NUM_FILES" -eq 0 ]; then
    echo "ERROR: No files downloaded at all"
    echo "failed" > ${{LOCAL_DIR}}/.status
    exit 1
fi

if [ "$FAILED_RUNS" -gt 0 ]; then
    echo "WARNING: $FAILED_RUNS run(s) failed but $NUM_FILES files downloaded — proceeding with partial data"
fi

echo "Download finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Clean up temp dir
rm -rf "${{LOCAL_DIR}}/tmp"

# Mark as ready for pickup by fir cron
date -u +%Y-%m-%dT%H:%M:%SZ > ${{LOCAL_DIR}}/.ready
echo "Staged and ready for pickup."

# Clear trap on successful exit
trap - EXIT
"""


async def submit_local_download_job(submission: Submission) -> str:
    """Stage scripts for download and mark as queued for the worker.

    No SSH required — pipeline.sh and download.sh are written locally.
    The omc-download-worker picks them up (via .queued marker) and runs
    them with concurrency control. After download, fir cron picks up
    the files over HTTP.

    Returns 'queued' as a placeholder.
    """
    if not settings.slurm_enabled:
        raise RuntimeError("SLURM not enabled")

    local_wrapper = _build_local_download_wrapper(submission)
    pipeline_script = _build_pipeline_script(submission)
    local_dir = f"{settings.local_download_path}/{submission.slug}"

    os.makedirs(local_dir, exist_ok=True)

    # Write pipeline.sh locally — fir cron will download it via HTTP
    with open(f"{local_dir}/pipeline.sh", "w") as f:
        f.write(pipeline_script)

    # Write download wrapper (worker will execute it)
    dl_path = f"{local_dir}/download.sh"
    with open(dl_path, "w") as f:
        f.write(local_wrapper)
    os.chmod(dl_path, 0o755)

    # Mark as queued for the download worker to pick up
    with open(f"{local_dir}/.queued", "w") as f:
        f.write(datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"))

    logger.info(f"Queued download for {submission.slug}")

    return "queued"


async def get_submission_status(slug: str) -> dict:
    """Get submission status from local staging markers + pushed HPC status.

    No SSH required. Status sources (checked in order):
      1. Local staging dir (.status / .ready) — download phase
      2. Pushed HPC status via /staging/{slug}/status — pipeline phase
    """
    from .staging import get_hpc_status

    # Check local staging first (download still in progress on arbutus)
    local_dir = Path(settings.local_download_path) / slug
    if local_dir.exists():
        local_ready = local_dir / ".ready"
        local_status = local_dir / ".status"
        local_queued = local_dir / ".queued"
        if local_ready.exists():
            return {"phase": "downloading", "detail": "Waiting for HPC pickup"}
        if local_status.exists():
            status = local_status.read_text().strip()
            if status == "downloading":
                return {"phase": "downloading"}
            elif status == "failed":
                return {"phase": "failed", "reason": "Local download failed"}
        if local_queued.exists():
            return {"phase": "downloading", "detail": "Queued for download"}

    # Check pushed HPC status (fir → arbutus over HTTP)
    hpc = get_hpc_status(slug)
    if hpc:
        return hpc

    # No info available yet
    return {"phase": "unknown"}


async def check_job_status(job_id: str) -> dict:
    """Check job status (placeholder-aware)."""
    if job_id.startswith("local-"):
        return {"job_id": job_id, "state": "DOWNLOADING", "running": True}
    # For real SLURM job IDs, check pushed status
    return {"job_id": job_id, "state": "UNKNOWN", "running": True}


async def check_completion_marker(slug: str) -> dict:
    """Check for completion via pushed HPC status."""
    from .staging import get_hpc_status

    hpc = get_hpc_status(slug)
    if hpc:
        phase = hpc.get("phase", "")
        if phase in ("completed", "failed"):
            exit_code = hpc.get("exit_code", "1" if phase == "failed" else "0")
            return {
                "completed": True,
                "success": phase == "completed",
                "exit_code": exit_code,
            }
    return {"completed": False}


async def cancel_job(job_id: str) -> bool:
    """Cancel a job."""
    if job_id.startswith("local-"):
        pid = job_id[6:]
        try:
            os.kill(int(pid), 15)  # SIGTERM
            return True
        except (ProcessLookupError, ValueError):
            return False
    # Can't cancel remote SLURM jobs without SSH
    logger.warning(f"Cannot cancel remote job {job_id} — no SSH available")
    return False


async def poll_all_running_jobs(db_session) -> list:
    """Poll for completed submissions using pushed HPC status.

    No SSH required — reads status files pushed by fir over HTTP.
    """
    from sqlalchemy import select
    from .database import Submission, SubmissionStatus
    from .staging import get_hpc_status

    stmt = select(Submission).where(
        Submission.status.in_([SubmissionStatus.QUEUED, SubmissionStatus.RUNNING])
    )
    result = await db_session.execute(stmt)
    running = result.scalars().all()

    completed = []
    for sub in running:
        hpc = get_hpc_status(sub.slug)
        if not hpc:
            continue

        phase = hpc.get("phase", "")
        job_id = hpc.get("job_id")

        # Update job ID if we got a real one
        if job_id and sub.slurm_job_id != job_id:
            sub.slurm_job_id = job_id

        # Update status based on phase
        if phase == "running" and sub.status != SubmissionStatus.RUNNING:
            sub.status = SubmissionStatus.RUNNING
        elif phase == "completed":
            sub.status = SubmissionStatus.PROCESSING
            logger.info(f"Submission {sub.slug} completed successfully")
            completed.append(sub.slug)
        elif phase == "failed":
            sub.status = SubmissionStatus.FAILED
            sub.error_message = hpc.get("reason", f"Exit code {hpc.get('exit_code', '?')}")
            logger.warning(f"Submission {sub.slug} failed: {sub.error_message}")
            completed.append(sub.slug)
        elif phase == "queued" and sub.status != SubmissionStatus.QUEUED:
            sub.status = SubmissionStatus.QUEUED

    await db_session.commit()

    return completed
