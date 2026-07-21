#!/bin/bash
#SBATCH --job-name=omc-pickup
#SBATCH --account=def-rec3141_cpu
#SBATCH --time=7-00:00:00
#SBATCH --cpus-per-task=1
#SBATCH --mem=1G
#SBATCH --output=/home/rec3141/scratch/omc-pickup.log
#SBATCH --error=/home/rec3141/scratch/omc-pickup.log

export OMC_STAGING_URL="https://microbial.opencommunity.science"
export OMC_STAGING_KEY="$(cat ~/.config/omc/staging-key)"
export OMC_SCRATCH="$HOME/scratch"
export OMC_RESULTS="$HOME/scratch/omc_results"
# Cluster-specific pipeline paths, propagated to each pipeline job (--export=ALL)
# so the portal's generated scripts stay portable. Override per cluster if the
# danaSeq/microscape install or reference DBs live elsewhere.
export OMC_GENICE="${OMC_GENICE:-$HOME/GENICE}"
export OMC_DB_DIR="${OMC_DB_DIR:-$HOME/scratch/databases}"

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
sbatch --dependency=afterany:"$SLURM_JOB_ID" --begin=now+5minutes \
    "$SCRIPT_DIR/omc-pickup-loop.sh" \
    && echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) queued successor (afterany:$SLURM_JOB_ID)"
# ---------------------------------------------------------------------------

while true; do
    "$SCRIPT_DIR/omc-pickup.sh" 2>&1
    sleep 300
done
