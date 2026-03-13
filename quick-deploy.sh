#!/bin/bash
# Quick deploy — sync code and restart service
# Usage: ./quick-deploy.sh
set -euo pipefail

HOST="206.12.96.115"
REMOTE="ubuntu@${HOST}"
DEST="/opt/omc-platform"

echo "--- Syncing code ---"
rsync -az --delete \
    --exclude '.venv' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.env' \
    --exclude '*.db' \
    --exclude '*.pem' \
    --exclude '.git' \
    ./ "${REMOTE}:${DEST}/"

echo "--- Restarting service ---"
ssh "$REMOTE" sudo systemctl restart omc-portal

echo "--- Done --- https://microbial.opencommunity.science"
