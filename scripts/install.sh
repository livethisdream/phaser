#!/usr/bin/env bash
# install.sh — Linux / macOS setup for Phaser Desktop
# =====================================================
# Usage:
#   bash scripts/install.sh          # pip + venv
#   INSTALLER=uv bash scripts/install.sh
#
# Linux system prerequisite for pywebview (WebKitGTK):
#   Ubuntu/Debian:  sudo apt install python3-gi gir1.2-webkit2-4.0
#   Fedora/RHEL:    sudo dnf install webkit2gtk4.0
#   Arch:           sudo pacman -S webkit2gtk
#
# macOS: No extra deps needed (WKWebView is built in).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
INSTALLER="${INSTALLER:-pip}"

cd "$REPO_ROOT"

# --------------------------------------------------------------------------
# Python backend
# --------------------------------------------------------------------------
if [ "$INSTALLER" = "uv" ]; then
    echo "==> Installing Python deps with uv..."
    uv sync
else
    echo "==> Creating/reusing .venv..."
    if [ ! -f ".venv/bin/python" ]; then
        python3 -m venv .venv
    fi
    VENV_PYTHON=".venv/bin/python"
    "$VENV_PYTHON" -m ensurepip --upgrade
    "$VENV_PYTHON" -m pip install --upgrade pip
    "$VENV_PYTHON" -m pip install -r requirements.txt
fi

# --------------------------------------------------------------------------
# Frontend
# --------------------------------------------------------------------------
if [ -d "frontend" ]; then
    echo "==> Building frontend..."
    cd frontend
    npm install
    npm run build
    cd "$REPO_ROOT"
else
    echo "WARNING: frontend/ directory not found — skipping frontend build."
fi

echo ""
echo "Install complete."
echo ""
echo "Run the app:"
echo "  Simulation:  bash scripts/start-app-sim.sh"
echo "  Hardware:    bash scripts/start-app-real.sh"
echo ""
echo "Or still use the web server mode:"
echo "  Simulation:  bash scripts/start-sim.sh"
echo "  Hardware:    bash scripts/start-real.sh"

