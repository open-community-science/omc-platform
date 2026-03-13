#!/bin/bash
# Deploy OMC Platform to Arbutus VM
# Usage: ./deploy.sh [host] [user]
set -euo pipefail

HOST="${1:-206.12.96.115}"
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
    sudo git clone https://github.com/rec3141/omc-platform.git /opt/omc-platform
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
ExecStart=/opt/omc-platform/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8002
Restart=always
RestartSec=5

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

    location / {
        proxy_pass http://127.0.0.1:8002;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300;
    }
}
NGINX

ssh "$REMOTE" bash <<'NGINX_ENABLE'
sudo ln -sf /etc/nginx/sites-available/omc-platform /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl restart nginx
NGINX_ENABLE

echo ""
echo "=== Deployment complete ==="
echo "HTTP:  http://${HOST}"
echo "Next:  Point ${DOMAIN} DNS to ${HOST}, then run:"
echo "  ssh ${REMOTE} sudo certbot --nginx -d ${DOMAIN}"
