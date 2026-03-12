#!/bin/bash
# Deploy OMC Portal
# Run from the omc-platform directory

set -e

APP_DIR="/opt/omc/omc-platform"
SERVICE_NAME="omc-portal"

echo "=== Deploying OMC Portal ==="

# Pull latest code
cd $APP_DIR
git pull origin main

# Activate venv and update deps
source venv/bin/activate
pip install -r portal/requirements.txt

# Create systemd service if not exists
if [ ! -f /etc/systemd/system/$SERVICE_NAME.service ]; then
    echo "Creating systemd service..."
    sudo tee /etc/systemd/system/$SERVICE_NAME.service > /dev/null << EOF
[Unit]
Description=OMC Portal
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=$APP_DIR/portal
Environment="PATH=$APP_DIR/venv/bin"
ExecStart=$APP_DIR/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable $SERVICE_NAME
fi

# Restart service
sudo systemctl restart $SERVICE_NAME

echo "=== Deployment complete ==="
echo "Check status: sudo systemctl status $SERVICE_NAME"
echo "View logs: sudo journalctl -u $SERVICE_NAME -f"
