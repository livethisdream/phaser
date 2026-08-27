#!/bin/bash
# Phaser Pi one-time provisioning.
#
# This script runs ON THE PI, as the service user, and needs NO sudo.
#   1. Installs Python dependencies phaser_headless.py needs (into ~/.local)
#   2. Ensures the install dir exists (it lives inside the user's own home)
#
# Staying sudo-free is load-bearing, not tidiness. setup.sh pipes this file in
# over `ssh -t ... 'bash -s' < setup-pi.sh`; with stdin redirected from a file
# ssh cannot allocate a pty, so any sudo that needs a password fails outright
# -- and on the stock Phaser image sudo-to-root does need one. The old
# `sudo mkdir`/`sudo chown` ran only when the install dir was missing, i.e.
# only on a genuinely new Pi, which is the one case this script exists for.
#
# It deliberately does NOT write the systemd unit. deploy.py owns that now:
# it renders scripts/phaser-headless.service.template from the same constants
# it scp's files with, and installs the unit when the Pi has none. This script
# used to carry a second heredoc copy of the unit, free to drift from the
# template and from deploy.py's REMOTE_DIR. setup.sh runs deploy.py as its last
# step, so the fresh-Pi path still ends with an installed, enabled service.
#
# Idempotent: safe to re-run.
#
# Typical invocation (from a laptop):
#   ssh analog@phaser.local 'bash -s' < setup-pi.sh
#
# The laptop-side setup.sh / setup.ps1 wrappers pipe this file over ssh
# automatically; you rarely need to run it by hand.

set -e

INSTALL_DIR="/home/analog/pyadi-iio/examples/phaser"
SERVICE_NAME="phaser-headless"
SERVICE_USER="analog"
PYTHON_BIN="/usr/bin/python3"

echo "=== Phaser Pi provisioning ==="
echo "Install dir : $INSTALL_DIR"
echo "Service     : $SERVICE_NAME (installed later, by deploy.py)"
echo "Python      : $PYTHON_BIN"
echo

# ---- sanity ------------------------------------------------------------------
if [ ! -x "$PYTHON_BIN" ]; then
    echo "ERROR: $PYTHON_BIN not found. This script expects the Phaser Pi image."
    exit 1
fi
if ! id "$SERVICE_USER" > /dev/null 2>&1; then
    echo "ERROR: user '$SERVICE_USER' does not exist. This script expects the Phaser Pi image."
    exit 1
fi
# Everything below writes into $SERVICE_USER's own home, so it must BE that
# user. Running as anyone else would put the packages in the wrong ~/.local
# and the service would start without them.
if [ "$(id -un)" != "$SERVICE_USER" ]; then
    echo "ERROR: this script must run as '$SERVICE_USER', but is running as '$(id -un)'."
    echo "       Connect as that user:  ssh $SERVICE_USER@<host>"
    exit 1
fi

# ---- Python deps -------------------------------------------------------------
echo "[1/2] Installing Python dependencies for user '$SERVICE_USER'..."
# pyadi-iio and numpy are already on the Phaser image. Install the rest.
#
# A bookworm-based image marks the system Python PEP 668 externally-managed, so
# a plain `pip install --user` exits non-zero and, under `set -e`, aborts
# provisioning entirely. Retry with --break-system-packages, which is the
# intended escape hatch for a single-purpose appliance like this one. Note that
# --user must stay: the unit runs as $SERVICE_USER, so the packages have to be
# importable by that user, not by root.
# Install only what is actually missing, and never --upgrade. This script's job
# is "make sure these three import", not "keep them latest": a workshop Pi that
# already works should not have a known-good pyzmq swapped under it because
# someone re-ran setup. Re-running on a provisioned Pi is now a no-op.
MISSING=""
for pair in zmq:pyzmq msgpack:msgpack websockets:websockets; do
    mod="${pair%%:*}"
    pkg="${pair##*:}"
    "$PYTHON_BIN" -c "import $mod" > /dev/null 2>&1 || MISSING="$MISSING $pkg"
done

if [ -z "$MISSING" ]; then
    echo "  all present; nothing to install"
elif ! "$PYTHON_BIN" -m pip install --user $MISSING; then
    echo "  pip refused; retrying with --break-system-packages (PEP 668 image)..."
    "$PYTHON_BIN" -m pip install --user --break-system-packages $MISSING
fi

# Verify through the exact interpreter the unit's ExecStart will use. A --user
# install lands in $HOME/.local, and if it landed anywhere the service cannot
# import from, the install still "succeeds" -- the failure shows up only as a
# crash loop after deploy.
echo "  Verifying imports as $SERVICE_USER..."
if ! "$PYTHON_BIN" -c "import zmq, msgpack, websockets"; then
    echo "ERROR: packages installed but not importable as $SERVICE_USER."
    echo "       The service would crash-loop. Check where pip put them:"
    echo "       $PYTHON_BIN -m pip show -f pyzmq"
    exit 1
fi
echo "  OK: zmq, msgpack, websockets importable"

# ---- install dir -------------------------------------------------------------
echo "[2/2] Ensuring install dir exists..."
# Inside $HOME, so no sudo and no chown needed -- it is created owned by us.
mkdir -p "$INSTALL_DIR"

echo
echo "=== Provisioning complete ==="
echo "No files are deployed to $INSTALL_DIR yet, and the systemd unit is not"
echo "installed. Return to the laptop and run:"
echo "    python deploy.py <this-pi-host>"
echo
echo "That will scp the Python + frontend files, install and enable"
echo "$SERVICE_NAME.service if the Pi has none, and start the service."
