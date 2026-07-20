#!/bin/bash
# Quick deploy — sync code and restart service
# Usage: ./quick-deploy.sh
set -euo pipefail

# Connect via the `arbutus` ssh-config alias so it picks up the right key
# (IdentityFile ~/.ssh/arbutus.pem). Override with OMC_DEPLOY_REMOTE if needed.
REMOTE="${OMC_DEPLOY_REMOTE:-arbutus}"
DEST="/opt/omc-platform"

echo "--- Syncing code ---"
rsync -az --delete \
    --exclude '.venv' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '__marimo__' \
    --exclude '.env' \
    --exclude '*.db' \
    --exclude '*.pem' \
    --exclude '.git' \
    ./ "${REMOTE}:${DEST}/"

echo "--- Restarting service ---"
ssh "$REMOTE" sudo systemctl restart omc-portal

echo "--- Done --- https://microbial.opencommunity.science"
