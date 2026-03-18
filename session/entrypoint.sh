#!/bin/bash
set -e

# Root paths for reverse proxy (set by session manager)
# nginx forwards full path, so apps must serve at their prefix
CHAT_ROOT="${CHAT_ROOT_PATH:-}"
NB_ROOT="${NB_ROOT_PATH:-}"

# Symlink pipeline viz data into the app shell directory
if [ -d "/data/viz/data" ]; then
    ln -sf /data/viz/data /app/viz/data
fi

# Start viz static server on port 8082
python3 /app/viz_server.py &

# Start marimo in edit mode on port 8081 (authors can modify and re-run cells)
marimo edit notebooks/explore.py --host 0.0.0.0 --port 8081 --headless --no-token \
    ${NB_ROOT:+--base-url "$NB_ROOT"} &

# Start chainlit on port 8080
chainlit run chat_app.py --host 0.0.0.0 --port 8080 --headless \
    ${CHAT_ROOT:+--root-path "$CHAT_ROOT"} &

# Exit if any child dies
wait -n
