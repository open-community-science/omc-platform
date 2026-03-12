#!/bin/bash
# Setup script for Alliance Canada Cloud VM
# Run this on a fresh Ubuntu 22.04 VM

set -e

echo "=== OMC Portal Setup ==="

# Update system
sudo apt-get update
sudo apt-get upgrade -y

# Install Python 3.11
sudo apt-get install -y software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt-get update
sudo apt-get install -y python3.11 python3.11-venv python3.11-dev

# Install system dependencies
sudo apt-get install -y nginx certbot python3-certbot-nginx git

# Create app directory
sudo mkdir -p /opt/omc
sudo chown $USER:$USER /opt/omc

# Clone repository (adjust URL as needed)
cd /opt/omc
# git clone https://github.com/omc-papers/omc-platform.git
# cd omc-platform

# Create virtual environment
python3.11 -m venv venv
source venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install -r portal/requirements.txt

# Create .env file template
cat > portal/.env << 'EOF'
# OMC Portal Configuration
DEBUG=false
SECRET_KEY=your-secret-key-here

# GitHub OAuth (create at https://github.com/settings/developers)
GITHUB_CLIENT_ID=
GITHUB_CLIENT_SECRET=
GITHUB_REDIRECT_URI=https://your-domain.com/auth/callback

# GitHub API token for repo creation
GITHUB_TOKEN=
GITHUB_ORG=omc-papers

# Claude API
ANTHROPIC_API_KEY=

# SLURM (Alliance Canada HPC)
SLURM_ENABLED=true
SLURM_HOST=cedar.computecanada.ca
SLURM_USER=your-username
SLURM_SSH_KEY=/home/ubuntu/.ssh/id_rsa
SLURM_PARTITION=def-alloc
SLURM_ACCOUNT=def-alloc
EOF

echo "=== Setup complete ==="
echo "Next steps:"
echo "1. Edit /opt/omc/portal/.env with your credentials"
echo "2. Set up systemd service (see deploy.sh)"
echo "3. Configure nginx and SSL"
