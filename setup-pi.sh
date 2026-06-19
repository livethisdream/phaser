#!/bin/bash
#
# Phaser Pi Setup Script
# Run this on the Raspberry Pi to install/update the Phaser backend
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/.../setup-pi.sh | bash
#   or
#   ./setup-pi.sh
#

set -e

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║              ADI Phaser Pi Setup Script                      ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# Check if running on Pi
if [[ ! -f /etc/os-release ]] || ! grep -q "Raspberry" /etc/os-release 2>/dev/null; then
    echo "Warning: This doesn't appear to be a Raspberry Pi"
    read -p "Continue anyway? [y/N] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# Default paths
INSTALL_DIR="/home/analog/pyadi-iio/examples/phaser"
SERVICE_NAME="phaser-headless"
FRONTEND_DIR="/var/www/phaser"

# Check for sudo
if [[ $EUID -ne 0 ]]; then
    SUDO="sudo"
else
    SUDO=""
fi

echo "[1/5] Installing Python dependencies..."
pip3 install --user pyzmq msgpack websockets 2>/dev/null || {
    echo "Trying with sudo..."
    $SUDO pip3 install pyzmq msgpack websockets
}

echo "[2/5] Creating directories..."
$SUDO mkdir -p "$INSTALL_DIR"
$SUDO mkdir -p "$FRONTEND_DIR"
$SUDO chown -R analog:analog "$FRONTEND_DIR" 2>/dev/null || true

echo "[3/5] Installing phaser_headless.py..."
# If running from repo, copy local file; otherwise download
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/phaser_headless.py" ]]; then
    $SUDO cp "$SCRIPT_DIR/phaser_headless.py" "$INSTALL_DIR/"
    echo "  Copied from local: $SCRIPT_DIR/phaser_headless.py"
else
    echo "  Error: phaser_headless.py not found in $SCRIPT_DIR"
    echo "  Please copy phaser_headless.py to $INSTALL_DIR manually"
fi

echo "[4/5] Installing systemd service..."
cat << 'EOF' | $SUDO tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null
[Unit]
Description=Phaser Headless Backend
After=network.target

[Service]
Type=simple
User=analog
WorkingDirectory=/home/analog/pyadi-iio/examples/phaser
ExecStart=/usr/bin/python3 -u /home/analog/pyadi-iio/examples/phaser/phaser_headless.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

$SUDO systemctl daemon-reload
$SUDO systemctl enable ${SERVICE_NAME}

echo "[5/5] Starting service..."
$SUDO systemctl restart ${SERVICE_NAME}
sleep 2

# Check if running
if systemctl is-active --quiet ${SERVICE_NAME}; then
    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║                    Setup Complete!                           ║"
    echo "╠══════════════════════════════════════════════════════════════╣"
    echo "║  Service Status: RUNNING                                     ║"
    echo "║                                                              ║"
    IP=$(hostname -I | awk '{print $1}')
    printf "║  Web UI:     http://%-15s:8080                   ║\n" "$IP"
    printf "║  WebSocket:  ws://%-15s:8765                     ║\n" "$IP"
    echo "║                                                              ║"
    echo "║  Commands:                                                   ║"
    echo "║    sudo systemctl status phaser-headless                     ║"
    echo "║    sudo journalctl -u phaser-headless -f                     ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
else
    echo ""
    echo "ERROR: Service failed to start"
    echo "Check logs with: sudo journalctl -u ${SERVICE_NAME} -n 50"
    exit 1
fi

echo ""
echo "To install the web frontend, copy the frontend/dist folder to:"
echo "  $FRONTEND_DIR"
echo ""
echo "Or serve from local development with:"
echo "  cd frontend && npm run dev"
