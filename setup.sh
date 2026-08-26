#!/bin/bash
# One-stop Phaser setup for Unix / macOS / WSL testers.
#
# From a fresh clone, this runs zero-to-working:
#   1. Verifies local prereqs (Python 3.11+, node, npm)
#   2. npm install + npm run build for the beamforming frontend
#   3. (optional) same for the radar frontend
#   4. ssh to the Pi and provision it (installs deps + systemd unit)
#   5. Runs deploy.py to copy files + start the service
#
# Usage:
#   ./setup.sh                       # Pi at phaser.local (default)
#   ./setup.sh 192.168.1.42          # Pi at a specific IP
#   ./setup.sh --skip-pi             # laptop side only (build frontends)
#
# The Pi must be reachable and your ssh key must already be authorized
# for user `analog`. Test with:  ssh analog@<host> 'echo ok'

set -e

HOST="${1:-}"
SKIP_PI=0
for arg in "$@"; do
    if [ "$arg" = "--skip-pi" ]; then
        SKIP_PI=1
    elif [ -z "$HOST" ] || [ "$arg" = "$HOST" ]; then
        HOST="$arg"
    fi
done
# Strip --skip-pi if it ended up in HOST
if [ "$HOST" = "--skip-pi" ]; then HOST=""; fi
HOST="${HOST:-phaser.local}"

echo "=================================================="
echo "  Phaser one-stop setup"
echo "  Pi target: $HOST"
[ "$SKIP_PI" = "1" ] && echo "  (--skip-pi: local build only)"
echo "=================================================="

# ---- prereqs -----------------------------------------------------------------
echo
echo "[1/4] Checking local prerequisites..."

need() {
    if ! command -v "$1" > /dev/null 2>&1; then
        echo "  ERROR: '$1' not found on PATH. Please install it and re-run."
        exit 1
    fi
    echo "  OK: $1 -> $(command -v "$1")"
}

need python3
need node
need npm

# Python >= 3.11
PYVER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
MAJOR=$(echo "$PYVER" | cut -d. -f1)
MINOR=$(echo "$PYVER" | cut -d. -f2)
if [ "$MAJOR" -lt 3 ] || { [ "$MAJOR" -eq 3 ] && [ "$MINOR" -lt 11 ]; }; then
    echo "  ERROR: Python 3.11+ required, found $PYVER"
    exit 1
fi
echo "  OK: python3 is $PYVER"

# ---- frontend builds ---------------------------------------------------------
echo
echo "[2/4] Building frontend(s)..."

if [ -d "frontend" ] && [ -f "frontend/package.json" ]; then
    echo "  Building beamforming frontend..."
    (cd frontend && npm install --silent && npm run build)
else
    echo "  ERROR: frontend/package.json missing. Are you at the repo root?"
    exit 1
fi

if [ -d "frontend-radar" ] && [ -f "frontend-radar/package.json" ]; then
    echo "  Building radar frontend..."
    (cd frontend-radar && npm install --silent && npm run build)
else
    echo "  (skip: no frontend-radar/ — radar UI won't be available)"
fi

if [ "$SKIP_PI" = "1" ]; then
    echo
    echo "=================================================="
    echo "  Local build complete. --skip-pi was set, so"
    echo "  the Pi was not provisioned or deployed to."
    echo "=================================================="
    exit 0
fi

# ---- ssh sanity check --------------------------------------------------------
echo
echo "[3/4] Provisioning Pi at analog@$HOST..."
if ! ssh -o BatchMode=yes -o ConnectTimeout=5 "analog@$HOST" 'echo ok' > /dev/null 2>&1; then
    echo "  ERROR: cannot ssh to analog@$HOST without a password."
    echo "  Fix: copy your ssh key with 'ssh-copy-id analog@$HOST', then retry."
    exit 1
fi
echo "  OK: passwordless ssh to analog@$HOST works"

# Pipe setup-pi.sh to the Pi. sudo may prompt for a password interactively
# via ssh -t; that's fine — the tester enters it once.
ssh -t "analog@$HOST" 'bash -s' < setup-pi.sh

# ---- deploy ------------------------------------------------------------------
echo
echo "[4/4] Deploying files..."
python3 deploy.py "$HOST"

echo
echo "=================================================="
echo "  Setup complete."
echo "  Open: http://$HOST:8080/"
echo "  (Instructor mode: http://$HOST:8080/?instructor=1)"
echo
echo "  Watch logs:  ssh analog@$HOST 'sudo journalctl -u phaser-headless -f'"
echo "  Redeploy:    python3 deploy.py $HOST"
echo "=================================================="
