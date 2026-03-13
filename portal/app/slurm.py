"""SLURM job submission and monitoring.

Uses OpenSSH CLI (not asyncssh) to leverage ControlMaster sockets,
which is required for Alliance Canada clusters with Duo MFA.

Job flow:
  1. Download runs on login node (fast internet) via nohup background process
  2. On success, download script submits pipeline sbatch job automatically
  3. Portal polls for status via marker files + squeue
"""
import asyncio
import logging
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


def _build_download_wrapper(submission: Submission) -> str:
    """Generate a wrapper script that runs on the login node.

    Downloads SRA data, then submits the pipeline sbatch job on success.
    Designed to run via `nohup ... &` on the login node.
    """
    scratch = settings.hpc_scratch
    output_dir = f"{settings.results_path}/{submission.slug}"
    input_dir = f"{scratch}/sra_downloads/{submission.slug}"
    accession = submission.bioproject_accession

    # Extract run accessions from selected_runs if available
    run_accessions = []
    if submission.selected_runs:
        for row in submission.selected_runs:
            if isinstance(row, dict) and row.get("run_accessions"):
                run_accessions.extend(row["run_accessions"])

    # Helper function: prefetch with timeout + single retry, then fasterq-dump
    download_fn = """
download_run() {
    local acc="$1"
    local TIMEOUT=1800  # 30 min per run

    echo "Downloading $acc..."
    if timeout $TIMEOUT prefetch "$acc" -O ${INPUT_DIR}; then
        echo "  prefetch OK for $acc"
    else
        echo "  prefetch failed or timed out for $acc — retrying once..."
        rm -rf "${INPUT_DIR}/${acc}" 2>/dev/null
        if timeout $TIMEOUT prefetch "$acc" -O ${INPUT_DIR}; then
            echo "  prefetch OK on retry for $acc"
        else
            echo "  FAILED: $acc (giving up after 1 retry)" | tee -a ${OUTPUT_DIR}/failed_runs.txt
            FAILED_RUNS=$((FAILED_RUNS + 1))
            return 1
        fi
    fi

    fasterq-dump "${INPUT_DIR}/${acc}/${acc}.sra" -O ${INPUT_DIR}/fastq -e 4 && \
    pigz -p 4 ${INPUT_DIR}/fastq/${acc}*.fastq
    if [ $? -ne 0 ]; then
        echo "  FAILED: fasterq-dump for $acc" | tee -a ${OUTPUT_DIR}/failed_runs.txt
        FAILED_RUNS=$((FAILED_RUNS + 1))
        return 1
    fi
    return 0
}

FAILED_RUNS=0
"""

    if run_accessions:
        download_cmd = download_fn + "\n".join(
            f'download_run "{acc}" || true'
            for acc in run_accessions
        )
    else:
        # Fallback: no explicit run accessions — download all runs for the BioProject
        # WARNING: this does not filter by platform, so mixed projects will download everything
        logger.warning(f"No run_accessions in selected_runs for {submission.slug} — downloading all runs")
        download_cmd = f"""{download_fn}
echo "WARNING: No explicit run accessions — downloading ALL runs for {accession}"
echo "Resolving runs for {accession}..."
module load gcc/12 edirect/20.9.20231210 2>/dev/null || true
esearch -db sra -query "{accession}" | efetch -format runinfo | \\
    awk -F',' 'NR>1 && $1 != "" {{print $1}}' > ${{INPUT_DIR}}/run_list.txt

NUM_RUNS=$(wc -l < ${{INPUT_DIR}}/run_list.txt)
echo "Found $NUM_RUNS runs to download"

while IFS= read -r acc; do
    download_run "$acc" || true
done < ${{INPUT_DIR}}/run_list.txt"""

    return f"""#!/bin/bash
# OMC Download Wrapper — runs on login node, submits pipeline on success
set -uo pipefail

INPUT_DIR="{input_dir}"
OUTPUT_DIR="{output_dir}"

# Trap: if the script dies for any reason, mark download as failed
cleanup_on_failure() {{
    local exit_code=$?
    if [ $exit_code -ne 0 ]; then
        echo "Download wrapper died with exit code $exit_code at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "download_failed" > ${{OUTPUT_DIR}}/.status
        echo "$exit_code" > ${{OUTPUT_DIR}}/.completed
    fi
}}
trap cleanup_on_failure EXIT

# Load SRA toolkit + Entrez Direct
module load gcc/12 sra-toolkit/3.0.9 edirect/20.9.20231210 2>/dev/null || true

mkdir -p "${{INPUT_DIR}}/fastq" "${{OUTPUT_DIR}}"

echo "=== OMC Download: {accession} ==="
echo "Slug: {submission.slug}"
echo "Started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Mark download in progress
echo "downloading" > ${{OUTPUT_DIR}}/.status

{download_cmd}

echo "Download complete. Files:"
ls -lh ${{INPUT_DIR}}/fastq/
NUM_FILES=$(ls ${{INPUT_DIR}}/fastq/*.fastq* 2>/dev/null | wc -l)
echo "Total: $NUM_FILES fastq files ($FAILED_RUNS failed)"

if [ "$NUM_FILES" -eq 0 ]; then
    echo "ERROR: No files downloaded at all"
    echo "download_failed" > ${{OUTPUT_DIR}}/.status
    echo 1 > ${{OUTPUT_DIR}}/.completed
    exit 1
fi

if [ "$FAILED_RUNS" -gt 0 ]; then
    echo "WARNING: $FAILED_RUNS run(s) failed but $NUM_FILES files downloaded — proceeding with partial data"
fi

echo "Download finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Submit pipeline job
echo "Submitting pipeline job..."
SBATCH_OUT=$(sbatch ${{OUTPUT_DIR}}/pipeline.sh)
PIPELINE_JOB_ID=$(echo "$SBATCH_OUT" | awk '{{print $NF}}')
echo "pipeline=$PIPELINE_JOB_ID" > ${{OUTPUT_DIR}}/job_ids.txt
echo "pipeline_queued" > ${{OUTPUT_DIR}}/.status
echo "Pipeline job submitted: $PIPELINE_JOB_ID"

# Clear trap on successful exit
trap - EXIT
"""


def _build_pipeline_script(submission: Submission) -> str:
    """Generate sbatch script for pipeline execution (heavy job)."""
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

# Trap SLURM signals (timeout/cancel) to write status markers
cleanup() {{
    echo "Signal caught — marking job as failed"
    echo "failed" > ${{OUTPUT_DIR}}/.status
    echo 1 > ${{OUTPUT_DIR}}/.completed
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
    exit 1
fi

echo "running" > ${{OUTPUT_DIR}}/.status

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
else
    echo "failed" > ${{OUTPUT_DIR}}/.status
fi
"""


def _ssh_cmd(remote_cmd: str, host: str = "") -> list[str]:
    """Build an SSH command using host config (ControlMaster via ~/.ssh/config)."""
    target_host = host or settings.slurm_host
    cmd = [
        "ssh",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=no",
    ]
    if settings.slurm_ssh_key:
        cmd += ["-i", settings.slurm_ssh_key]
    cmd += [f"{settings.slurm_user}@{target_host}", remote_cmd]
    return cmd


async def _run_remote(cmd: str, host: str = "") -> tuple[str, str, int]:
    """Run a command on the remote HPC via OpenSSH (uses ControlMaster)."""
    proc = await asyncio.create_subprocess_exec(
        *_ssh_cmd(cmd, host=host),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return stdout.decode(), stderr.decode(), proc.returncode or 0


async def submit_pipeline_job(submission: Submission) -> str:
    """Submit download (login node) + pipeline (sbatch) job chain.

    Returns 'downloading' as a placeholder — the real SLURM job ID
    is written to job_ids.txt by the download wrapper when it submits
    the pipeline job.
    """
    if not settings.slurm_enabled:
        raise RuntimeError("SLURM not enabled")

    download_wrapper = _build_download_wrapper(submission)
    pipeline_script = _build_pipeline_script(submission)
    output_dir = f"{settings.results_path}/{submission.slug}"

    if not settings.slurm_host:
        raise RuntimeError("Remote SLURM host required for job submission")

    # Create output directory
    await _run_remote(f"mkdir -p {output_dir}")

    # Write pipeline sbatch script (submitted by download wrapper on success)
    run_path = f"{output_dir}/pipeline.sh"
    await _run_remote(
        f"cat > {run_path} << 'SBATCH_EOF'\n{pipeline_script}\nSBATCH_EOF"
    )

    # Write download wrapper
    dl_path = f"{output_dir}/download.sh"
    await _run_remote(
        f"cat > {dl_path} << 'SBATCH_EOF'\n{download_wrapper}\nSBATCH_EOF"
    )
    await _run_remote(f"chmod +x {dl_path}")

    # Run download on login node (use dedicated download host if configured)
    dl_host = settings.slurm_download_host or ""
    stdout, stderr, rc = await _run_remote(
        f"nohup bash {dl_path} > {output_dir}/download.log 2>&1 &"
        f" echo $!",
        host=dl_host,
    )
    if rc != 0:
        raise RuntimeError(f"Failed to start download: {stderr}")

    dl_pid = stdout.strip()
    logger.info(
        f"Started download (PID {dl_pid}) on login node for {submission.slug}"
    )

    # Return a placeholder — real job ID comes later via job_ids.txt
    return f"dl-{dl_pid}"


async def get_submission_status(slug: str) -> dict:
    """Get detailed status by reading marker files on HPC.

    Status flow:
      downloading → pipeline_queued → running → completed/failed
    """
    output_dir = f"{settings.results_path}/{slug}"

    # Check .completed first (terminal state)
    stdout, _, rc = await _run_remote(f"cat {output_dir}/.completed 2>/dev/null")
    if rc == 0:
        exit_code = stdout.strip()
        return {
            "phase": "completed" if exit_code == "0" else "failed",
            "exit_code": exit_code,
        }

    # Check .status marker
    stdout, _, rc = await _run_remote(f"cat {output_dir}/.status 2>/dev/null")
    status = stdout.strip() if rc == 0 else ""

    # Check for pipeline job ID
    stdout, _, rc = await _run_remote(f"cat {output_dir}/job_ids.txt 2>/dev/null")
    job_info = {}
    if rc == 0:
        for part in stdout.strip().split():
            if "=" in part:
                k, v = part.split("=", 1)
                job_info[k] = v

    pipeline_job_id = job_info.get("pipeline")

    if pipeline_job_id:
        # Check SLURM job status
        stdout, _, rc = await _run_remote(
            f"squeue -j {pipeline_job_id} -h -o '%T' 2>/dev/null"
        )
        slurm_state = stdout.strip().strip("'") if rc == 0 else ""

        if slurm_state:
            return {
                "phase": "running" if slurm_state == "RUNNING" else "queued",
                "slurm_state": slurm_state,
                "job_id": pipeline_job_id,
            }
        else:
            # Job finished but no .completed marker yet — check sacct
            stdout, _, _ = await _run_remote(
                f"sacct -j {pipeline_job_id} -n -o State -P 2>/dev/null"
            )
            sacct_state = stdout.strip().split("\n")[0] if stdout.strip() else ""
            if sacct_state in ("COMPLETED", "FAILED", "CANCELLED", "TIMEOUT"):
                return {
                    "phase": "failed" if sacct_state != "COMPLETED" else "completed",
                    "slurm_state": sacct_state,
                    "job_id": pipeline_job_id,
                }

    if status == "downloading":
        return {"phase": "downloading"}
    elif status == "download_failed":
        return {"phase": "failed", "reason": "Download failed"}
    elif status == "pipeline_queued":
        return {"phase": "queued", "job_id": pipeline_job_id}

    return {"phase": "unknown", "status_marker": status}


async def check_job_status(job_id: str) -> dict:
    """Check SLURM job status via SSH (legacy interface)."""
    if settings.slurm_host:
        # Handle download placeholder IDs
        if job_id.startswith("dl-"):
            return {"job_id": job_id, "state": "DOWNLOADING", "running": True}

        stdout, _, rc = await _run_remote(
            f"squeue -j {job_id} -h -o '%T' 2>/dev/null"
        )
        state = stdout.strip().strip("'") if rc == 0 else ""

        if not state:
            stdout2, _, _ = await _run_remote(
                f"sacct -j {job_id} -n -o State -P 2>/dev/null"
            )
            state = stdout2.strip().split("\n")[0] if stdout2.strip() else "UNKNOWN"
            return {"job_id": job_id, "state": state, "running": False}

        return {
            "job_id": job_id,
            "state": state,
            "running": state in ("PENDING", "RUNNING", "CONFIGURING"),
        }
    else:
        proc = await asyncio.create_subprocess_exec(
            "squeue", "-j", job_id, "-h", "-o", "%T",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        state = stdout.decode().strip()

        if not state:
            return {"job_id": job_id, "state": "COMPLETED", "running": False}

        return {
            "job_id": job_id,
            "state": state,
            "running": state in ("PENDING", "RUNNING", "CONFIGURING"),
        }


async def check_completion_marker(slug: str) -> dict:
    """Check for .completed marker file — used by the daily poll."""
    marker_path = f"{settings.results_path}/{slug}/.completed"

    if settings.slurm_host:
        stdout, _, rc = await _run_remote(f"cat {marker_path} 2>/dev/null")
        if rc == 0:
            exit_code = stdout.strip()
            return {
                "completed": True,
                "success": exit_code == "0",
                "exit_code": exit_code,
            }
        return {"completed": False}
    else:
        marker = Path(marker_path)
        if marker.exists():
            exit_code = marker.read_text().strip()
            return {
                "completed": True,
                "success": exit_code == "0",
                "exit_code": exit_code,
            }
        return {"completed": False}


async def cancel_job(job_id: str) -> bool:
    """Cancel a SLURM job."""
    if job_id.startswith("dl-"):
        # Kill login node download process
        pid = job_id[3:]
        _, _, rc = await _run_remote(f"kill {pid} 2>/dev/null")
        return rc == 0

    if settings.slurm_host:
        _, _, rc = await _run_remote(f"scancel {job_id}")
        return rc == 0
    else:
        proc = await asyncio.create_subprocess_exec(
            "scancel", job_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return proc.returncode == 0


async def poll_all_running_jobs(db_session) -> list:
    """
    Poll: check all running submissions for completion markers.
    Also updates job IDs from download wrapper when pipeline job is submitted.
    Returns list of newly completed submission slugs.
    """
    from sqlalchemy import select
    from .database import Submission, SubmissionStatus

    stmt = select(Submission).where(
        Submission.status.in_([SubmissionStatus.QUEUED, SubmissionStatus.RUNNING])
    )
    result = await db_session.execute(stmt)
    running = result.scalars().all()

    completed = []
    for sub in running:
        # Try to pick up the real SLURM job ID if we only have a download placeholder
        if sub.slurm_job_id and sub.slurm_job_id.startswith("dl-"):
            output_dir = f"{settings.results_path}/{sub.slug}"
            stdout, _, rc = await _run_remote(
                f"cat {output_dir}/job_ids.txt 2>/dev/null"
            )
            if rc == 0 and "pipeline=" in stdout:
                for part in stdout.strip().split():
                    if part.startswith("pipeline="):
                        sub.slurm_job_id = part.split("=", 1)[1]
                        sub.status = SubmissionStatus.QUEUED
                        await db_session.commit()
                        break

        status = await check_completion_marker(sub.slug)
        if status["completed"]:
            if status["success"]:
                sub.status = SubmissionStatus.PROCESSING
                logger.info(f"Submission {sub.slug} completed successfully")
            else:
                sub.status = SubmissionStatus.FAILED
                sub.error_message = f"Pipeline exited with code {status['exit_code']}"
                logger.warning(
                    f"Submission {sub.slug} failed with exit code {status['exit_code']}"
                )
            completed.append(sub.slug)

    if completed:
        await db_session.commit()

    return completed
