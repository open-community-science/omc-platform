"""SLURM job submission and monitoring via asyncssh."""
import asyncio
import asyncssh
import logging
from typing import Optional
from pathlib import Path

from .config import get_settings
from .database import Submission, PipelineType

settings = get_settings()
logger = logging.getLogger(__name__)


def _get_pipeline_path(pipeline: PipelineType) -> str:
    """Return the HPC path for a given pipeline type.

    Raises NotImplementedError for pipelines that are not yet deployed.
    """
    pipeline_paths = {
        PipelineType.NANOPORE_MAG: settings.pipeline_nanopore_mag,
        PipelineType.MICROSCAPE: settings.pipeline_microscape,
        PipelineType.ILLUMINA_MAG: settings.pipeline_illumina_mag,
        PipelineType.RNASEQ: settings.pipeline_rnaseq,
        PipelineType.ISOLATE_GENOME: settings.pipeline_isolate_genome,
    }

    path = pipeline_paths.get(pipeline)
    if path is None:
        raise NotImplementedError(
            f"Pipeline '{pipeline.value}' is not yet supported for SLURM submission."
        )

    # Log warning for pipelines that may not be deployed yet
    if pipeline in (PipelineType.ILLUMINA_MAG, PipelineType.RNASEQ, PipelineType.ISOLATE_GENOME):
        logger.warning(
            f"Pipeline '{pipeline.value}' may not be deployed on HPC yet. "
            f"Expected path: {path}"
        )

    return path


def _build_sbatch_script(submission: Submission) -> str:
    """Generate sbatch script for the pipeline."""
    pipeline_path = _get_pipeline_path(submission.pipeline)

    output_dir = f"{settings.results_path}/{submission.id}"

    script = f"""#!/bin/bash
#SBATCH --job-name=omc-{submission.id}
#SBATCH --account={settings.slurm_account}
#SBATCH --partition={settings.slurm_partition}
#SBATCH --time=24:00:00
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --output={output_dir}/slurm-%j.out
#SBATCH --error={output_dir}/slurm-%j.err

# Load required modules
module load nextflow/23.10.0
module load singularity/3.8.6

# Create output directory
mkdir -p {output_dir}

# Run pipeline
cd {output_dir}
nextflow run {pipeline_path}/main.nf \\
    --bioproject_accession {submission.bioproject_accession} \\
    --outdir {output_dir}/results \\
    -profile singularity,slurm \\
    -resume

# Signal completion (with exit code)
echo $? > {output_dir}/.completed
"""
    return script


async def _get_ssh_connection() -> asyncssh.SSHClientConnection:
    """Create SSH connection to HPC cluster."""
    connect_kwargs = {
        "host": settings.slurm_host,
        "username": settings.slurm_user,
        "known_hosts": None,  # TODO: use known_hosts file in production
    }

    if settings.slurm_ssh_key:
        connect_kwargs["client_keys"] = [settings.slurm_ssh_key]

    return await asyncssh.connect(**connect_kwargs)


async def _run_remote(cmd: str) -> tuple[str, str, int]:
    """Run a command on the remote HPC and return (stdout, stderr, exit_code)."""
    async with await _get_ssh_connection() as conn:
        result = await conn.run(cmd)
        return result.stdout or "", result.stderr or "", result.exit_status or 0


async def submit_pipeline_job(submission: Submission) -> str:
    """Submit a Nextflow pipeline job to SLURM."""
    if not settings.slurm_enabled:
        raise RuntimeError("SLURM not enabled")

    script = _build_sbatch_script(submission)
    output_dir = f"{settings.results_path}/{submission.id}"

    if settings.slurm_host:
        # Remote submission via asyncssh
        async with await _get_ssh_connection() as conn:
            # Create output directory
            await conn.run(f"mkdir -p {output_dir}")

            # Write sbatch script
            script_path = f"{output_dir}/submit.sh"
            await conn.run(f"cat > {script_path} << 'SBATCH_EOF'\n{script}\nSBATCH_EOF")

            # Submit
            result = await conn.run(f"sbatch {script_path}")

            if result.exit_status != 0:
                raise RuntimeError(f"sbatch failed: {result.stderr}")

            # Parse job ID from "Submitted batch job 12345"
            output = (result.stdout or "").strip()
            job_id = output.split()[-1]
            logger.info(f"Submitted job {job_id} for submission {submission.id}")
            return job_id
    else:
        # Local submission (when portal runs on HPC login node)
        proc = await asyncio.create_subprocess_exec(
            "sbatch",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(script.encode())

        if proc.returncode != 0:
            raise RuntimeError(f"sbatch failed: {stderr.decode()}")

        output = stdout.decode().strip()
        job_id = output.split()[-1]
        return job_id


async def check_job_status(job_id: str) -> dict:
    """Check SLURM job status via SSH."""
    if settings.slurm_host:
        stdout, stderr, rc = await _run_remote(f"squeue -j {job_id} -h -o %T 2>/dev/null")
        state = stdout.strip()

        if not state:
            # Job not in queue — check sacct
            stdout2, _, _ = await _run_remote(f"sacct -j {job_id} -n -o State -P 2>/dev/null")
            state = stdout2.strip().split("\n")[0] if stdout2.strip() else "UNKNOWN"
            return {"job_id": job_id, "state": state, "running": False}

        return {
            "job_id": job_id,
            "state": state,
            "running": state in ("PENDING", "RUNNING", "CONFIGURING"),
        }
    else:
        # Local fallback
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


async def check_completion_marker(submission_id: int) -> dict:
    """Check for .completed marker file — used by the daily poll."""
    marker_path = f"{settings.results_path}/{submission_id}/.completed"

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
    Daily poll: check all running submissions for completion markers.
    Called by a cron job or scheduled task.
    Returns list of newly completed submission IDs.
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
        status = await check_completion_marker(sub.id)
        if status["completed"]:
            if status["success"]:
                sub.status = SubmissionStatus.PROCESSING
                logger.info(f"Submission {sub.id} completed successfully")
            else:
                sub.status = SubmissionStatus.FAILED
                sub.error_message = f"Pipeline exited with code {status['exit_code']}"
                logger.warning(f"Submission {sub.id} failed with exit code {status['exit_code']}")
            completed.append(sub.id)

    if completed:
        await db_session.commit()

    return completed
