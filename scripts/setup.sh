#!/bin/bash
# One-stop Phaser setup for Unix / macOS / WSL testers.
#
# This is the FIRST-TIME provisioning path: it installs the Python deps on the
# Pi, which deploy.py does not do. After running it once, `python deploy.py` is
# all you need for every subsequent update. (The systemd unit is deploy.py's
# job either way -- it installs one whenever the Pi has none.)
#
#   1. Verifies local prereqs (Python 3.11+; node/npm only if building)
#   2. Uses the committed frontend build, or builds it if absent/--build
#   3. ssh to the Pi and provision it (installs Python deps)
#   4. Runs deploy.py to copy files, install the unit, start the service
#
# Usage:
#   ./scripts/setup.sh                  # Pi at phaser.local (default)
#   ./scripts/setup.sh 192.168.1.42     # Pi at a specific IP
#   ./scripts/setup.sh --skip-pi        # laptop side only
#   ./scripts/setup.sh --build          # force a frontend rebuild (needs Node)
#
# The Pi must be reachable and your ssh key must already be authorized
# for user `analog`. Test with:  ssh analog@<host> 'echo ok'

set -e

# This script lives in scripts/ but every path below is repo-root relative,
# so anchor to the root regardless of where it was invoked from.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."

HOST=""
SKIP_PI=0
WANT_BUILD=0
for arg in "$@"; do
    case "$arg" in
        --skip-pi) SKIP_PI=1 ;;
        --build)   WANT_BUILD=1 ;;
        --*)       echo "  ERROR: unknown option '$arg'"; exit 1 ;;
        *)         [ -z "$HOST" ] && HOST="$arg" ;;
    esac
done
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

# node/npm are NOT required. frontend/dist is committed (built by CI), so the
# normal path has no toolchain at all. They are only checked if we actually
# have to build -- see the next step.
have() { command -v "$1" > /dev/null 2>&1; }

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
echo "[2/4] Frontend..."

if [ ! -f "frontend/dist/index.html" ]; then
    echo "  No committed build found; it will have to be built."
    WANT_BUILD=1
fi

if [ "$WANT_BUILD" = "1" ]; then
    if ! have node || ! have npm; then
        echo "  ERROR: building needs node + npm, which are not on PATH."
        if [ -f "frontend/dist/index.html" ]; then
            echo "  Drop --build to use the committed build instead."
        else
            echo "  Install Node, or check out a commit that has frontend/dist/."
        fi
        exit 1
    fi
    echo "  Building beamforming frontend..."
    (cd frontend && npm install --silent && npm run build)
    if [ -f "frontend-radar/package.json" ]; then
        echo "  Building radar frontend..."
        (cd frontend-radar && npm install --silent && npm run build)
    fi
else
    echo "  OK: using the committed build (no Node required)"
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
ssh -t "analog@$HOST" 'bash -s' < "$SCRIPT_DIR/setup-pi.sh"

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
