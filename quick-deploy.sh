#!/bin/bash
# Quick deploy — sync code and restart service
# Usage: ./quick-deploy.sh
set -euo pipefail

# Connect via the `arbutus` ssh-config alias so it picks up the right key
# (IdentityFile ~/.ssh/arbutus.pem). Override with OMC_DEPLOY_REMOTE if needed.
REMOTE="${OMC_DEPLOY_REMOTE:-arbutus}"
DEST="/opt/omc-platform"
DEPLOY_REF="${OMC_DEPLOY_REF:-origin/main}"

# rsync ships the working tree, not HEAD, so a stale or dirty checkout will
# silently revert whatever landed on the branch since you last pulled. That has
# already happened once: a deploy from an out-of-date tree took the dashboard
# list styling and a landing-page CTA back off production for half an hour.
# Refuse to deploy unless the tree is exactly $DEPLOY_REF.
if [ "${OMC_DEPLOY_FORCE:-0}" = "1" ]; then
    echo "--- OMC_DEPLOY_FORCE=1: skipping the up-to-date check ---"
else
    echo "--- Checking the tree matches ${DEPLOY_REF} ---"
    git rev-parse --git-dir >/dev/null 2>&1 || {
        echo "ERROR: not a git repository — cannot verify what would be deployed." >&2
        echo "       Set OMC_DEPLOY_FORCE=1 to deploy anyway." >&2
        exit 1
    }
    remote_name="${DEPLOY_REF%%/*}"
    git fetch --quiet "$remote_name" || {
        echo "ERROR: could not fetch ${remote_name}; refusing to deploy blind." >&2
        echo "       Set OMC_DEPLOY_FORCE=1 to deploy anyway." >&2
        exit 1
    }

    dirty="$(git status --porcelain)"
    if [ -n "$dirty" ]; then
        echo "ERROR: working tree has uncommitted changes:" >&2
        printf '%s\n' "$dirty" | sed 's/^/         /' >&2
        echo "       Commit or stash them, or set OMC_DEPLOY_FORCE=1 to deploy as-is." >&2
        exit 1
    fi

    head_sha="$(git rev-parse HEAD)"
    ref_sha="$(git rev-parse "$DEPLOY_REF")"
    if [ "$head_sha" != "$ref_sha" ]; then
        echo "ERROR: HEAD is not ${DEPLOY_REF}." >&2
        echo "         HEAD          $(git rev-parse --short=8 HEAD)  $(git log -1 --format=%s HEAD)" >&2
        echo "         ${DEPLOY_REF}   $(git rev-parse --short=8 "$DEPLOY_REF")  $(git log -1 --format=%s "$DEPLOY_REF")" >&2
        behind="$(git rev-list --count "HEAD..${DEPLOY_REF}")"
        ahead="$(git rev-list --count "${DEPLOY_REF}..HEAD")"
        echo "       ${behind} commit(s) behind, ${ahead} ahead." >&2
        [ "$behind" -gt 0 ] && echo "       Deploying now would revert those ${behind} commit(s) on the server." >&2
        echo "       Run: git checkout main && git pull --ff-only" >&2
        echo "       Or set OMC_DEPLOY_FORCE=1 to deploy this tree anyway." >&2
        exit 1
    fi
    echo "    OK — $(git rev-parse --short=8 HEAD) matches ${DEPLOY_REF}"
fi

# Excludes are load-bearing in two directions: they keep local junk off the
# server, and because rsync never deletes an excluded path, they also protect
# the server's own files from --delete. The patterns are deliberately wider than
# the literal names — '.env' alone does not match '.env.bak-20260724', and
# '*.db' does not match 'omc.db.bak-pre-rerun11'; both of those are real files
# on the server that --delete would otherwise destroy. Likewise '.venv' does not
# match a local '.venv-test'.
RSYNC_EXCLUDES=(
    --exclude '.git'
    --exclude '.github'
    --exclude '.claude'
    --exclude '.venv*'          # .venv, .venv-test, any local virtualenv
    --exclude '__pycache__'
    --exclude '*.pyc'
    --exclude '__marimo__'
    --exclude 'node_modules'
    --exclude '.DS_Store'
    --exclude '.env*'           # server env file AND its backups
    --exclude '*.db'            # server database
    --exclude '*.db.*'          # and its backups: omc.db.bak-*
    --exclude '*.bak*'
    --exclude '*.pem'
    --exclude '*.sqsh'
    # Anything git ignores locally has no business on the server either.
    --filter=':- .gitignore'
)

echo "--- Previewing changes ---"
deletions="$(rsync -azn --delete --itemize-changes "${RSYNC_EXCLUDES[@]}" \
    ./ "${REMOTE}:${DEST}/" | grep '^\*deleting' || true)"
if [ -n "$deletions" ]; then
    echo "This deploy would DELETE files on the server:" >&2
    printf '%s\n' "$deletions" | sed 's/^/    /' >&2
    if [ "${OMC_DEPLOY_ALLOW_DELETE:-0}" != "1" ]; then
        echo "  Refusing to delete. Review the list above; if it is correct," >&2
        echo "  re-run with OMC_DEPLOY_ALLOW_DELETE=1." >&2
        exit 1
    fi
    echo "  OMC_DEPLOY_ALLOW_DELETE=1 — proceeding with the deletions above."
fi

echo "--- Syncing code ---"
rsync -az --delete "${RSYNC_EXCLUDES[@]}" ./ "${REMOTE}:${DEST}/"

echo "--- Restarting service ---"
ssh "$REMOTE" sudo systemctl restart omc-portal

echo "--- Done --- https://microbial.opencommunity.science"
