#!/bin/bash
set -e

# Root paths for reverse proxy (set by session manager)
# nginx forwards full path, so apps must serve at their prefix
CHAT_ROOT="${CHAT_ROOT_PATH:-}"
NB_ROOT="${NB_ROOT_PATH:-}"

# Start marimo in run mode on port 8081
marimo run notebooks/explore.py --host 0.0.0.0 --port 8081 --headless \
    ${NB_ROOT:+--base-url "$NB_ROOT"} &

# Start chainlit on port 8080
exec chainlit run chat_app.py --host 0.0.0.0 --port 8080 --headless \
    ${CHAT_ROOT:+--root-path "$CHAT_ROOT"}
