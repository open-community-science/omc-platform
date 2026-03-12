"""SLURM job submission and monitoring."""
import asyncio
import subprocess
from typing import Optional
from datetime import datetime

from .config import get_settings
from .database import Submission, PipelineType

settings = get_settings()


def _build_sbatch_script(submission: Submission) -> str:
    """Generate sbatch script for the pipeline."""
    pipeline_path = (
        settings.pipeline_nanopore_mag
        if submission.pipeline == PipelineType.NANOPORE_MAG
        else settings.pipeline_microscape
    )

    script = f"""#!/bin/bash
#SBATCH --job-name=omc-{submission.id}
#SBATCH --account={settings.slurm_account}
#SBATCH --partition={settings.slurm_partition}
#SBATCH --time=24:00:00
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --output={settings.results_path}/{submission.id}/slurm-%j.out
#SBATCH --error={settings.results_path}/{submission.id}/slurm-%j.err

# Load required modules
module load nextflow/23.10.0
module load singularity/3.8.6

# Create output directory
mkdir -p {settings.results_path}/{submission.id}

# Run pipeline
cd {settings.results_path}/{submission.id}
nextflow run {pipeline_path}/main.nf \\
    --sra_accession {submission.sra_accession} \\
    --outdir {settings.results_path}/{submission.id}/results \\
    -profile singularity,slurm \\
    -resume

# Signal completion
touch {settings.results_path}/{submission.id}/.completed
"""
    return script


async def submit_pipeline_job(submission: Submission) -> str:
    """Submit a Nextflow pipeline job to SLURM."""
    if not settings.slurm_enabled:
        raise RuntimeError("SLURM not enabled")

    script = _build_sbatch_script(submission)

    # For SSH submission to remote HPC
    # This would use paramiko or asyncssh in production
    # For now, assume local sbatch (testing) or raise
    if settings.slurm_host:
        # Remote submission via SSH
        raise NotImplementedError("Remote SLURM submission not yet implemented")
    else:
        # Local submission (for testing on HPC login node)
        proc = await asyncio.create_subprocess_exec(
            "sbatch",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(script.encode())

        if proc.returncode != 0:
            raise RuntimeError(f"sbatch failed: {stderr.decode()}")

        # Parse job ID from "Submitted batch job 12345"
        output = stdout.decode().strip()
        job_id = output.split()[-1]
        return job_id


async def check_job_status(job_id: str) -> dict:
    """Check SLURM job status."""
    proc = await asyncio.create_subprocess_exec(
        "squeue",
        "-j", job_id,
        "-h",
        "-o", "%T",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        # Job not found - check sacct for completed jobs
        proc2 = await asyncio.create_subprocess_exec(
            "sacct",
            "-j", job_id,
            "-n",
            "-o", "State",
            "-P",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout2, _ = await proc2.communicate()
        state = stdout2.decode().strip().split("\n")[0] if stdout2 else "UNKNOWN"
        return {"job_id": job_id, "state": state, "running": False}

    state = stdout.decode().strip()
    return {
        "job_id": job_id,
        "state": state or "COMPLETED",
        "running": state in ("PENDING", "RUNNING", "CONFIGURING"),
    }


async def cancel_job(job_id: str) -> bool:
    """Cancel a SLURM job."""
    proc = await asyncio.create_subprocess_exec(
        "scancel", job_id,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    return proc.returncode == 0
