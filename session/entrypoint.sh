#!/bin/bash
set -e

# Start marimo in run mode (app mode, no code editing) on port 8081
marimo run notebooks/explore.py --host 0.0.0.0 --port 8081 --headless &

# Start chainlit on port 8080
chainlit run chat_app.py --host 0.0.0.0 --port 8080 --headless
