#!/bin/bash
# Deploy OMC Platform to Arbutus VM
# Usage: ./deploy.sh [host] [user]
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
sudo apt-get install -y -qq \
    python3-pip python3-venv python3-dev \
    nginx certbot python3-certbot-nginx \
    git build-essential libffi-dev libssl-dev \
    sqlite3
SETUP

# Step 2: Clone/update repo
echo "--- Setting up repository ---"
ssh "$REMOTE" bash <<'REPO'
set -euo pipefail
if [ -d /opt/omc-platform ]; then
    cd /opt/omc-platform
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
echo "--- Copying configuration ---"
scp portal/.env "${REMOTE}:/opt/omc-platform/portal/.env"

# Step 5: Copy GitHub App private key
echo "--- Copying GitHub App key ---"
scp omc-platform.2026-03-12.private-key.pem "${REMOTE}:/opt/omc-platform/"

# Step 6: Update .env paths for production
ssh "$REMOTE" bash <<'ENVFIX'
set -euo pipefail
cd /opt/omc-platform/portal
# Fix paths for production
sed -i 's|/data/omc/omc-platform|/opt/omc-platform|g' .env
# Set production values
sed -i 's|DEBUG=true|DEBUG=false|' .env
sed -i "s|SECRET_KEY=change-me-in-production|SECRET_KEY=$(openssl rand -hex 32)|" .env
# Update redirect URI for production domain
sed -i 's|GITHUB_REDIRECT_URI=.*|GITHUB_REDIRECT_URI=https://microbial.opencommunity.science/auth/callback|' .env
ENVFIX

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
