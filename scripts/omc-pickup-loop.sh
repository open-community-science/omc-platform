#!/bin/bash
#SBATCH --job-name=omc-pickup
#SBATCH --account=def-rec3141_cpu
#SBATCH --time=7-00:00:00
#SBATCH --cpus-per-task=1
# 1G is right for a polling loop and wrong for one that occasionally builds a
# SIF. `singularity pull` converts the OCI layers with mksquashfs, which peaks at
# ~2.75 GB for the amplicon image; under a 1G cgroup it was killed mid-build every
# cycle for four days while the log said only "pull FAILED".
#SBATCH --mem=6G
#SBATCH --output=/home/rec3141/scratch/omc-pickup.log
#SBATCH --error=/home/rec3141/scratch/omc-pickup.log

# Per-cluster overrides (account, container runtime, paths, module loads) so the
# same script runs on every cluster. See scripts/cluster.env.example.
[ -f "$HOME/.config/omc/cluster.env" ] && . "$HOME/.config/omc/cluster.env"

export OMC_STAGING_URL="https://microbial.opencommunity.science"
export OMC_STAGING_KEY="$(cat ~/.config/omc/staging-key)"
export OMC_SCRATCH="${OMC_SCRATCH:-$HOME/scratch}"
export OMC_RESULTS="${OMC_RESULTS:-$OMC_SCRATCH/omc_results}"
# Cluster-specific pipeline paths, propagated to each pipeline job (--export=ALL)
# so the portal's generated scripts stay portable. Override per cluster if the
# danaSeq/illumina_amplicon install or reference DBs live elsewhere.
export OMC_GENICE="${OMC_GENICE:-$HOME/GENICE}"
export OMC_DB_DIR="${OMC_DB_DIR:-$OMC_SCRATCH/databases}"
# Container runtime for the amplicon SIF, which the portal's script invokes
# directly. grex has SingularityCE behind a module and no apptainer at all.
export OMC_APPTAINER="${OMC_APPTAINER:-apptainer}"
export OMC_ACCOUNT="${OMC_ACCOUNT:-}"

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]:-$0}")")" && pwd)"
# Under sbatch, $0 points at SLURM's spool copy of this script (path differs per
# cluster: /localscratch/… on fir, /var/spool/slurmd/… on nibi, etc.), so the
# sibling omc-pickup.sh isn't there. Detect that by absence and fall back to the
# repo path derived from OMC_GENICE (portable across clusters).
if [ ! -f "$SCRIPT_DIR/omc-pickup.sh" ]; then
    SCRIPT_DIR="${OMC_GENICE:-$HOME/GENICE}/omc-platform/scripts"
fi

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) omc-pickup loop started (PID $$, job $SLURM_JOB_ID)"

# --- Watchdog: dependency-chained succession -------------------------------
# Immediately register a successor that SLURM will launch when THIS job ends for
# ANY reason (time-limit exit, crash, node failure, preemption, or a scancel of
# just this job). Because the successor lives in SLURM's DB before we do any work,
# no single job death can break the chain.
#
# This replaces the old `sleep 6d23h && sbatch` self-resubmit, which was lost
# whenever the job died before its timer fired — that failure silently stopped
# all pipeline pickups for ~4 months (Mar–Jul 2026).
#
# --begin=now+5min throttles pathological fast-fail restart loops to <=1 / 5 min;
# it's already in the past for a normal 7-day run, so it adds no delay then.
#
# To stop the system intentionally you must cancel BOTH the running job AND the
# pending dependent successor:  scancel --name=omc-pickup -u "$USER"
#
# The successor must repeat the account override the first submission was given:
# the #SBATCH directive in this file names fir's allocation, and a cluster whose
# cluster.env corrects it would otherwise lose the correction the first time the
# chain rolled over.
LOOP_SBATCH=()
[ -n "$OMC_ACCOUNT" ] && LOOP_SBATCH+=(--account="$OMC_ACCOUNT")
if sbatch "${LOOP_SBATCH[@]}" --dependency=afterany:"$SLURM_JOB_ID" --begin=now+5minutes \
        "$SCRIPT_DIR/omc-pickup-loop.sh"; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) queued successor (afterany:$SLURM_JOB_ID)"
else
    # The chain is the only thing keeping pickups alive across a job's death, so
    # a failure here is not a warning — say so loudly rather than run seven days
    # and stop. A cluster whose batch jobs start without SLURM's own bin on PATH
    # fails exactly here; see the PATH section of scripts/cluster.env.example.
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ERROR: could not queue a successor —" \
         "this loop will NOT restart when it ends (sbatch: $(command -v sbatch || echo 'not on PATH'))"
fi
# ---------------------------------------------------------------------------

while true; do
    "$SCRIPT_DIR/omc-pickup.sh" 2>&1
    sleep 300
done
