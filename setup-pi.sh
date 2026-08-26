#!/bin/bash
# Phaser Pi one-time provisioning.
#
# This script runs ON THE PI. It:
#   1. Installs Python dependencies phaser_headless.py needs
#   2. Writes /etc/systemd/system/phaser-headless.service
#   3. Enables the service (does not start it — deploy.py will do that
#      after scp'ing the actual Python files)
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
echo "Service     : $SERVICE_NAME"
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

# ---- Python deps -------------------------------------------------------------
echo "[1/3] Installing Python dependencies for user '$SERVICE_USER'..."
# pyadi-iio and numpy are already on the Phaser image. Install the rest.
sudo -u "$SERVICE_USER" "$PYTHON_BIN" -m pip install --user --upgrade \
    pyzmq msgpack websockets

# ---- install dir -------------------------------------------------------------
echo "[2/3] Ensuring install dir exists..."
if [ ! -d "$INSTALL_DIR" ]; then
    sudo mkdir -p "$INSTALL_DIR"
    sudo chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
fi

# ---- systemd unit ------------------------------------------------------------
echo "[3/3] Installing systemd unit..."
sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" > /dev/null <<EOF
[Unit]
Description=Phaser Headless Backend (browser-hosted beamforming + CW radar)
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$PYTHON_BIN -u $INSTALL_DIR/phaser_headless.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"

echo
echo "=== Provisioning complete ==="
echo "The service is enabled but not running yet (no files deployed"
echo "to $INSTALL_DIR). Return to the laptop and run:"
echo "    python deploy.py <this-pi-host>"
echo
echo "That will scp the Python + frontend files and restart the service."
