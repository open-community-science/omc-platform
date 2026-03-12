#!/bin/bash
# OMC Platform - Portable Install Script
# Works on Linux and macOS with Python 3.12+
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORTAL_DIR="$SCRIPT_DIR/portal"
VENV_DIR="$PORTAL_DIR/.venv"

echo "=== OMC Platform Install ==="

# Check Python version
PYTHON=""
for cmd in python3.12 python3.13 python3; do
    if command -v "$cmd" &>/dev/null; then
        ver=$("$cmd" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 12 ]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "ERROR: Python 3.12+ is required but not found."
    echo "Install it via:"
    echo "  macOS:  brew install python@3.12"
    echo "  Ubuntu: sudo apt install python3.12 python3.12-venv"
    echo "  Conda:  conda install python=3.12"
    exit 1
fi

echo "Using $PYTHON ($($PYTHON --version))"

# Create virtual environment
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    "$PYTHON" -m venv "$VENV_DIR"
else
    echo "Virtual environment already exists at $VENV_DIR"
fi

# Activate and install
source "$VENV_DIR/bin/activate"
pip install --upgrade pip --quiet

echo "Installing portal dependencies..."
pip install -e "$PORTAL_DIR" --quiet

echo "Installing AI module..."
pip install -e "$PORTAL_DIR" --quiet  # ai/ is imported as a subpackage

# Set up .env if missing
if [ ! -f "$PORTAL_DIR/.env" ]; then
    cp "$PORTAL_DIR/.env.example" "$PORTAL_DIR/.env"
    echo "Created $PORTAL_DIR/.env from template — edit it with your credentials."
fi

echo ""
echo "=== Install complete ==="
echo ""
echo "Activate the environment:"
echo "  source $VENV_DIR/bin/activate"
echo ""
echo "Run the portal:"
echo "  cd $PORTAL_DIR && uvicorn app.main:app --reload"
echo ""
echo "Configuration:"
echo "  Edit $PORTAL_DIR/.env"
