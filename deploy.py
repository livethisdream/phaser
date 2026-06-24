#!/usr/bin/env python3
"""
Deploy Phaser Beamforming app to Raspberry Pi.

Usage:
    python deploy.py                    # Deploy to default host (192.168.86.20)
    python deploy.py 192.168.1.100      # Deploy to specific host
    python deploy.py --build-only       # Just build frontend, don't deploy
"""

import subprocess
import sys
import os
from pathlib import Path

# Default Pi settings
DEFAULT_HOST = "phaser.local"
DEFAULT_USER = "analog"
REMOTE_DIR = "/home/analog/pyadi-iio/examples/phaser"
REMOTE_WWW = f"{REMOTE_DIR}/frontend/dist"

def run(cmd, cwd=None, check=True):
    """Run a command and print it."""
    print(f"  $ {cmd}")
    result = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=False)
    if check and result.returncode != 0:
        print(f"  ERROR: Command failed with exit code {result.returncode}")
        sys.exit(1)
    return result

def main():
    script_dir = Path(__file__).parent.resolve()
    frontend_dir = script_dir / "frontend"
    dist_dir = frontend_dir / "dist"

    # Parse args
    build_only = "--build-only" in sys.argv
    host = DEFAULT_HOST
    for arg in sys.argv[1:]:
        if not arg.startswith("--"):
            host = arg
            break

    print("=" * 60)
    print("  Phaser Beamforming Deployment")
    print("=" * 60)

    # Step 1: Build frontend
    print("\n[1/4] Building frontend...")
    if not (frontend_dir / "package.json").exists():
        print("  ERROR: frontend/package.json not found")
        sys.exit(1)
    run("npm run build", cwd=frontend_dir)

    if not (dist_dir / "index.html").exists():
        print("  ERROR: Build failed - dist/index.html not found")
        sys.exit(1)
    print("  OK: Frontend built")

    if build_only:
        print("\n--build-only specified, skipping deployment.")
        return

    # Step 2: Copy backend Python files
    print(f"\n[2/4] Copying backend scripts to {host}...")
    backend_files = [
        "phaser_headless.py",
        "phaser_cal_headless.py",
        "phaser_find_hb100_headless.py",
    ]
    for filename in backend_files:
        py_file = script_dir / filename
        if py_file.exists():
            run(f'scp "{py_file}" {DEFAULT_USER}@{host}:{REMOTE_DIR}/')
            print(f"  OK: {filename} copied")
        else:
            print(f"  SKIP: {filename} not found")

    # Step 3: Copy frontend
    print(f"\n[3/4] Copying frontend to {host}:{REMOTE_WWW}/...")
    run(f'ssh {DEFAULT_USER}@{host} "mkdir -p {REMOTE_WWW}"')
    run(f'scp -r "{dist_dir}"/* {DEFAULT_USER}@{host}:{REMOTE_WWW}/')
    print("  OK: Frontend copied")

    # Step 4: Restart service
    print(f"\n[4/4] Restarting phaser-headless service...")
    print("  NOTE: You may need to enter the sudo password on the Pi")
    result = run(
        f'ssh -t {DEFAULT_USER}@{host} "sudo systemctl restart phaser-headless"',
        check=False
    )
    if result.returncode == 0:
        print("  OK: Service restarted")
    else:
        print("  WARN: Could not restart service automatically.")
        print(f"        SSH into {host} and run: sudo systemctl restart phaser-headless")

    # Done
    print("\n" + "=" * 60)
    print(f"  Deployment complete!")
    print(f"  Open http://{host}:8080 in your browser")
    print("=" * 60)

if __name__ == "__main__":
    main()
