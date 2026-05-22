#!/usr/bin/env bash
# start-app-sim.sh — Launch Phaser desktop app (simulation mode)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# Prefer venv python; fall back to system python3
if [ -f ".venv/bin/python" ]; then
    PYTHON=".venv/bin/python"
elif command -v uv &>/dev/null; then
    PYTHON="uv run python"
else
    PYTHON="python3"
fi

exec $PYTHON phaser_app.py --sim "$@"

