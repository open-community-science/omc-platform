#!/bin/bash
#SBATCH --job-name=omc-pickup
#SBATCH --account=def-rec3141
#SBATCH --time=7-00:00:00
#SBATCH --cpus-per-task=1
#SBATCH --mem=1G
#SBATCH --output=/home/rec3141/scratch/omc-pickup.log
#SBATCH --error=/home/rec3141/scratch/omc-pickup.log

export OMC_STAGING_URL="https://microbial.opencommunity.science"
export OMC_STAGING_KEY="$(cat ~/.config/omc/staging-key)"
export OMC_SCRATCH="$HOME/scratch"
export OMC_RESULTS="$HOME/scratch/omc_results"

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]:-$0}")")" && pwd)"
# Fallback: if running inside SLURM spool, use the known repo path
[[ "$SCRIPT_DIR" == /localscratch/* ]] && SCRIPT_DIR="/home/rec3141/GENICE/omc-platform/scripts"

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) omc-pickup loop started (PID $$, job $SLURM_JOB_ID)"

# Self-resubmit before SLURM kills us (6d 23h = leave 1h margin)
(sleep $((6*86400 + 23*3600)) && sbatch "$SCRIPT_DIR/omc-pickup-loop.sh" && echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) Self-resubmitted") &

while true; do
    "$SCRIPT_DIR/omc-pickup.sh" 2>&1
    sleep 300
done
