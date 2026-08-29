#!/usr/bin/env python3
"""
Build a self-contained installer package for Phaser Pi.

LEGACY. The supported path is install.sh, run on the Pi -- it does provisioning
and update in one idempotent pass, and needs nothing on the client but ssh.
This exists only for handing someone a single tarball with no repo and no
network. It is not exercised by CI.

Creates: phaser-installer.tar.gz in the repo root.
Copy to any Phaser Pi and run:
    tar xzf phaser-installer.tar.gz
    cd phaser-installer
    ./install.sh
"""

import subprocess
import sys
import os
import shutil
import tarfile
from pathlib import Path

def run(cmd, cwd=None):
    print(f"  $ {cmd}")
    result = subprocess.run(cmd, shell=True, cwd=cwd)
    if result.returncode != 0:
        print(f"  ERROR: Command failed")
        sys.exit(1)

# This script lives in scripts/; everything it packages is relative to the
# repo root, one level up.
REPO_ROOT = Path(__file__).parent.parent.resolve()

# phaser_headless.py imports these at module top. Shipping the entrypoint
# alone produced a tarball that only ran on a Pi that already happened to
# have them from pyadi-iio's own examples directory.
BACKEND_FILES = [
    "phaser_headless.py",
    "phaser_cal_headless.py",
    "phaser_find_hb100_headless.py",
    "phaser_cw_radar.py",
    "ADAR_pyadi_functions.py",
    "SDR_functions.py",
    "phaser_functions.py",
    "LTE5_MHz.ftr",
    "LTE10_MHz.ftr",
    "LTE20_MHz.ftr",
]


def main():
    script_dir = REPO_ROOT
    frontend_dir = script_dir / "frontend"
    dist_dir = frontend_dir / "dist"

    # Output
    staging_dir = script_dir / "phaser-installer"
    output_file = script_dir / "phaser-installer.tar.gz"

    print("=" * 60)
    print("  Building Phaser Installer Package")
    print("=" * 60)

    # Step 1: Build frontend
    print("\n[1/3] Building frontend...")
    run("npm run build", cwd=frontend_dir)

    if not (dist_dir / "index.html").exists():
        print("  ERROR: Build failed")
        sys.exit(1)

    # Step 2: Create staging directory
    print("\n[2/3] Staging files...")
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir()

    # Copy backend (entrypoint + every module it imports, as one set)
    for name in BACKEND_FILES:
        src = script_dir / name
        if src.exists():
            shutil.copy(src, staging_dir / name)
        else:
            print(f"  WARN: {name} not found, omitting from the package")

    # Copy frontend
    www_dir = staging_dir / "www"
    shutil.copytree(dist_dir, www_dir)

    # Create install script
    install_script = staging_dir / "install.sh"
    install_script.write_text('''#!/bin/bash
set -e

INSTALL_DIR="/home/analog/pyadi-iio/examples/phaser"
SERVICE_NAME="phaser-headless"

echo "========================================"
echo "  Phaser Beamforming Installer"
echo "========================================"

# Check if running as analog user or root
if [[ $EUID -eq 0 ]]; then
    SUDO=""
else
    SUDO="sudo"
fi

echo ""
echo "[1/5] Installing Python dependencies..."
pip3 install --user pyzmq msgpack websockets 2>/dev/null || $SUDO pip3 install pyzmq msgpack websockets

echo ""
echo "[2/5] Copying backend..."
$SUDO mkdir -p "$INSTALL_DIR"
$SUDO cp phaser_headless.py "$INSTALL_DIR/"
$SUDO chown analog:analog "$INSTALL_DIR/phaser_headless.py"

echo ""
echo "[3/5] Copying frontend..."
$SUDO mkdir -p "$INSTALL_DIR/www"
$SUDO cp -r www/* "$INSTALL_DIR/www/"
$SUDO chown -R analog:analog "$INSTALL_DIR/www"

echo ""
echo "[4/5] Installing systemd service..."
$SUDO tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null << 'EOF'
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

echo ""
echo "[5/5] Starting service..."
$SUDO systemctl restart ${SERVICE_NAME}
sleep 2

if systemctl is-active --quiet ${SERVICE_NAME}; then
    IP=$(hostname -I | awk '{print $1}')
    echo ""
    echo "========================================"
    echo "  Installation Complete!"
    echo "========================================"
    echo ""
    echo "  Web UI:  http://${IP}:8080"
    echo ""
    echo "  Commands:"
    echo "    sudo systemctl status phaser-headless"
    echo "    sudo journalctl -u phaser-headless -f"
    echo ""
else
    echo ""
    echo "ERROR: Service failed to start"
    echo "Check: sudo journalctl -u phaser-headless -n 50"
    exit 1
fi
''')
    install_script.chmod(0o755)

    # Create uninstall script
    uninstall_script = staging_dir / "uninstall.sh"
    uninstall_script.write_text('''#!/bin/bash
echo "Stopping and removing phaser-headless service..."
sudo systemctl stop phaser-headless 2>/dev/null || true
sudo systemctl disable phaser-headless 2>/dev/null || true
sudo rm -f /etc/systemd/system/phaser-headless.service
sudo systemctl daemon-reload
echo "Done. Files in /home/analog/pyadi-iio/examples/phaser/ were not removed."
''')
    uninstall_script.chmod(0o755)

    # Step 3: Create tarball
    print("\n[3/3] Creating installer package...")
    with tarfile.open(output_file, "w:gz") as tar:
        tar.add(staging_dir, arcname="phaser-installer")

    # Cleanup staging (may fail on OneDrive, that's ok)
    try:
        shutil.rmtree(staging_dir)
    except PermissionError:
        print("  Note: Could not remove staging dir (OneDrive sync). You can delete phaser-installer/ manually.")

    size_kb = output_file.stat().st_size / 1024
    print(f"\n  Created: {output_file.name} ({size_kb:.0f} KB)")

    print("\n" + "=" * 60)
    print("  To install on a Phaser Pi:")
    print("=" * 60)
    print(f"  1. Copy phaser-installer.tar.gz to the Pi")
    print(f"  2. tar xzf phaser-installer.tar.gz")
    print(f"  3. cd phaser-installer")
    print(f"  4. ./install.sh")
    print("")

if __name__ == "__main__":
    main()
