#!/bin/bash
# Bootstrap a NEW OMC Platform host (packages, repo, venv, systemd, nginx).
# Usage: ./deploy.sh [host] [user]
#
# This is not the script for shipping a code change to a running server — use
# ./quick-deploy.sh for that. Several steps here are destructive to an existing
# host: they overwrite the nginx site config and the systemd unit, and copy the
# local .env over the server's. The nginx step now refuses to clobber a config
# that differs (see OMC_DEPLOY_NGINX below), because the live one is
# Certbot-managed and this script emits a plain HTTP bootstrap config.
#
# Opt-in switches, all default to off / leave-server-alone:
#   OMC_DEPLOY_NGINX=1   replace an existing nginx site config (backs it up)
#   OMC_DEPLOY_ENV=1     replace the server's portal/.env (backs it up)
#   OMC_DEPLOY_KEY=path  install this GitHub App .pem
set -euo pipefail

HOST="${1:-134.87.12.190}"
USER="${2:-ubuntu}"
REMOTE="${USER}@${HOST}"
DOMAIN="microbial.opencommunity.science"

echo "=== Deploying OMC Platform to ${REMOTE} ==="

# Step 1: System packages
echo "--- Installing system packages ---"
ssh "$REMOTE" bash <<'SETUP'
set -euo pipefail
sudo apt-get update -qq
# Kept in step with CLAUDE.md > "Host Environment > System Packages". Without
# docker/squashfuse/fuse3 the portal serves pages but cannot mount pipeline
# results or launch author sessions; without sra-toolkit it cannot stage SRA
# downloads.
sudo apt-get install -y -qq \
    python3-pip python3-venv python3-dev \
    nginx certbot python3-certbot-nginx \
    git build-essential libffi-dev libssl-dev \
    sqlite3 \
    docker.io \
    squashfs-tools squashfuse fuse3 \
    sra-toolkit
SETUP

# Step 2: Clone/update repo
echo "--- Setting up repository ---"
ssh "$REMOTE" bash <<'REPO'
set -euo pipefail
if [ -d /opt/omc-platform ]; then
    cd /opt/omc-platform
    # A dirty tree makes `git pull` fail mid-run with a wall of git output. Say
    # what is actually wrong. Files rsynced by quick-deploy.sh show up here.
    if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
        echo "ERROR: /opt/omc-platform has uncommitted changes to tracked files:" >&2
        git status --porcelain --untracked-files=no | sed 's/^/         /' >&2
        echo "       Reconcile them first, e.g.:" >&2
        echo "         ssh <host> 'cd /opt/omc-platform && git fetch origin && git reset --hard origin/main'" >&2
        exit 1
    fi
    sudo git pull origin main
else
    sudo git clone https://github.com/open-community-science/omc-platform.git /opt/omc-platform
fi
sudo chown -R ubuntu:ubuntu /opt/omc-platform
REPO

# Step 3: Python environment
echo "--- Setting up Python environment ---"
ssh "$REMOTE" bash <<'PYTHON'
set -euo pipefail
cd /opt/omc-platform/portal
python3 -m venv /opt/omc-platform/.venv
source /opt/omc-platform/.venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt
PYTHON

# Step 4: Copy .env (secrets — not in git)
#
# The server's .env is the live production config: real API keys, the staging
# token fir authenticates with, the SECRET_KEY every active login cookie is
# signed against. The local copy is a developer's, pointing at a LAN LLM and a
# dev database. Copying it up unconditionally replaces production config with
# whoever ran the script last — the same clobber-by-default shape as an rsync
# with the wrong excludes. So seed it only when the server has none.
echo "--- Copying configuration ---"
PLACED_ENV=0
if ssh "$REMOTE" 'test -f /opt/omc-platform/portal/.env'; then
    if [ "${OMC_DEPLOY_ENV:-0}" = "1" ]; then
        [ -f portal/.env ] || { echo "ERROR: OMC_DEPLOY_ENV=1 but no local portal/.env to send." >&2; exit 1; }
        stamp="$(date +%Y%m%d-%H%M%S)"
        ssh "$REMOTE" "cp /opt/omc-platform/portal/.env /opt/omc-platform/portal/.env.bak-${stamp}"
        echo "    Existing .env backed up to portal/.env.bak-${stamp}"
        scp -q portal/.env "${REMOTE}:/opt/omc-platform/portal/.env"
        PLACED_ENV=1
        echo "    OMC_DEPLOY_ENV=1 — replaced the server's .env."
    else
        echo "    Server already has portal/.env — leaving it alone."
        echo "    (OMC_DEPLOY_ENV=1 replaces it from this machine, keeping a backup)"
    fi
else
    [ -f portal/.env ] || {
        echo "ERROR: the server has no portal/.env and there is none here to seed it with." >&2
        echo "       Create portal/.env from the table in CLAUDE.md > Configuration." >&2
        exit 1
    }
    scp -q portal/.env "${REMOTE}:/opt/omc-platform/portal/.env"
    PLACED_ENV=1
    echo "    Seeded portal/.env from this machine."
fi

# Step 5: Copy GitHub App private key
# Same rule, and the filename is not hardcoded — the key rotates, and a script
# naming one issue date goes stale the first time it does.
echo "--- Copying GitHub App key ---"
if ssh "$REMOTE" 'ls /opt/omc-platform/*.pem >/dev/null 2>&1'; then
    echo "    Server already has a GitHub App key — leaving it alone."
    echo "    (OMC_DEPLOY_KEY=/path/to/key.pem installs a different one)"
    if [ -n "${OMC_DEPLOY_KEY:-}" ]; then
        [ -f "$OMC_DEPLOY_KEY" ] || { echo "ERROR: no such key: $OMC_DEPLOY_KEY" >&2; exit 1; }
        scp -q "$OMC_DEPLOY_KEY" "${REMOTE}:/opt/omc-platform/"
        echo "    Installed $(basename "$OMC_DEPLOY_KEY")."
    fi
else
    key="${OMC_DEPLOY_KEY:-$(ls ./*.private-key.pem 2>/dev/null | head -1)}"
    [ -n "$key" ] && [ -f "$key" ] || {
        echo "ERROR: the server has no GitHub App key and none was found here." >&2
        echo "       Point OMC_DEPLOY_KEY at the .pem downloaded from the GitHub App." >&2
        exit 1
    }
    scp -q "$key" "${REMOTE}:/opt/omc-platform/"
    echo "    Installed $(basename "$key")."
fi

# Step 6: Point a freshly seeded .env at production
#
# Only when this run placed the file. Re-running these against a live .env
# rewrites settings the server may have been tuned away from by hand — the
# LLM_BASE_URL there is a reverse SSH tunnel on localhost, not what a checkout
# ships. The SECRET_KEY line only matches the placeholder, so it mints a key on
# a fresh install and never rotates a real one out from under live sessions.
if [ "$PLACED_ENV" = "1" ]; then
    ssh "$REMOTE" DOMAIN="$DOMAIN" bash <<'ENVFIX'
set -euo pipefail
cd /opt/omc-platform/portal
sed -i 's|/data/omc/omc-platform|/opt/omc-platform|g' .env
sed -i 's|DEBUG=true|DEBUG=false|' .env
sed -i "s|SECRET_KEY=change-me-in-production|SECRET_KEY=$(openssl rand -hex 32)|" .env
sed -i "s|GITHUB_REDIRECT_URI=.*|GITHUB_REDIRECT_URI=https://${DOMAIN}/auth/callback|" .env
ENVFIX
    echo "    Rewrote paths, DEBUG and the OAuth redirect for production."
else
    echo "    Left the server's .env untouched (nothing seeded this run)."
fi

# Step 7: systemd service
echo "--- Setting up systemd service ---"
ssh "$REMOTE" sudo tee /etc/systemd/system/omc-portal.service > /dev/null <<'SERVICE'
[Unit]
Description=OMC Portal (FastAPI)
After=network.target

[Service]
Type=exec
User=ubuntu
Group=ubuntu
WorkingDirectory=/opt/omc-platform/portal
Environment=PATH=/opt/omc-platform/.venv/bin:/usr/local/bin:/usr/bin
ExecStart=/opt/omc-platform/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8002 --http httptools
Restart=always
RestartSec=5
# The portal spawns squashfuse daemons to mount session results at
# /mnt/omc-sessions/<slug>. With the default KillMode=control-group, systemd
# kills every process in the cgroup on stop/restart — so each deploy tore down
# all live session mounts and running sessions started failing with
# "[Errno 107] Transport endpoint is not connected: '/data'". KillMode=process
# stops only uvicorn and leaves the mounts (and session containers) intact.
KillMode=process

[Install]
WantedBy=multi-user.target
SERVICE

ssh "$REMOTE" bash <<'SYSTEMD'
sudo systemctl daemon-reload
sudo systemctl enable omc-portal
sudo systemctl restart omc-portal
SYSTEMD

# Step 8: nginx reverse proxy
echo "--- Configuring nginx ---"
# The config below is a plain-HTTP bootstrap for a fresh host. A live host's
# config is Certbot-managed and may carry blocks this script knows nothing about
# (a /sampletown/ proxy, for one). Overwriting it would drop TLS and take those
# blocks offline — which is how sites-available and sites-enabled drifted apart
# on the legacy host. So: back up, diff, and stop unless told otherwise.
if ssh "$REMOTE" 'test -f /etc/nginx/sites-available/omc-platform'; then
    stamp="$(date +%Y%m%d-%H%M%S)"
    ssh "$REMOTE" "sudo cp /etc/nginx/sites-available/omc-platform \
        /etc/nginx/sites-available/omc-platform.bak-${stamp}"
    echo "    Existing config backed up to omc-platform.bak-${stamp}"
    if ssh "$REMOTE" 'sudo grep -q "managed by Certbot" /etc/nginx/sites-available/omc-platform'; then
        echo "WARNING: the live config is managed by Certbot (TLS)." >&2
        echo "         This script writes a plain HTTP config — applying it drops HTTPS" >&2
        echo "         until you re-run: sudo certbot --nginx -d ${DOMAIN}" >&2
    fi
    if [ "${OMC_DEPLOY_NGINX:-0}" != "1" ]; then
        echo "ERROR: refusing to overwrite an existing nginx config." >&2
        echo "       Review the backup above, then re-run with OMC_DEPLOY_NGINX=1" >&2
        echo "       if you really mean to replace it." >&2
        exit 1
    fi
    echo "    OMC_DEPLOY_NGINX=1 — replacing the existing config."
fi
ssh "$REMOTE" sudo tee /etc/nginx/sites-available/omc-platform > /dev/null <<NGINX
server {
    listen 80;
    server_name ${DOMAIN} ${HOST};
    client_max_body_size 0;

    # Upload endpoint — tuned for large streaming transfers (up to 50G+)
    location /staging/ {
        proxy_pass http://127.0.0.1:8002;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        # HTTP/1.1 upstream — required for chunked transfer encoding
        proxy_http_version 1.1;
        proxy_set_header Connection "";

        # Stream request body directly to uvicorn, no disk buffering
        proxy_request_buffering off;

        # Fallback: if nginx still buffers, use the data volume (not root disk)
        proxy_temp_path /data/nginx-tmp 1 2;

        # Long timeouts for multi-GB uploads
        proxy_read_timeout 3600;
        proxy_send_timeout 3600;
        proxy_connect_timeout 60;
    }

    location /relay/ {
        proxy_pass http://127.0.0.1:8484/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
    }

    location / {
        proxy_pass http://127.0.0.1:8002;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_read_timeout 300;
    }

    # Session container proxy — forwards /session-proxy/{port}/* to localhost:{port}/*
    # Does NOT strip the prefix; the apps handle their own root path. Chainlit and
    # Marimo are WebSocket-driven, so the Upgrade headers here are load-bearing:
    # without this block author sessions do not work at all.
    location ~ ^/session-proxy/(\d+)/ {
        proxy_pass http://127.0.0.1:\$1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400;
    }
}
NGINX

ssh "$REMOTE" bash <<'NGINX_ENABLE'
sudo ln -sf /etc/nginx/sites-available/omc-platform /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
# Nginx temp dirs on data volume (root disk too small for large upload buffering)
sudo mkdir -p /data/nginx-tmp
sudo chown www-data:www-data /data/nginx-tmp
# Set global client_body_temp_path in nginx.conf if not already present
sudo grep -q 'client_body_temp_path' /etc/nginx/nginx.conf || \
    sudo sed -i '/sendfile on;/i\\tclient_body_temp_path /data/nginx-tmp 1 2;' /etc/nginx/nginx.conf
sudo nginx -t && sudo systemctl restart nginx
NGINX_ENABLE

echo ""
echo "=== Deployment complete ==="
echo "HTTP:  http://${HOST}"
echo "Next:  Point ${DOMAIN} DNS to ${HOST}, then run:"
echo "  ssh ${REMOTE} sudo certbot --nginx -d ${DOMAIN}"
