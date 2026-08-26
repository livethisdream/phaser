#!/usr/bin/env python3
"""
Deploy the Phaser browser UI to the Raspberry Pi.

The built frontends are committed to the repo (built by CI, see
.github/workflows/build-frontends.yml), so deploying does NOT build by
default — a fresh clone can deploy with no Node installed at all.

Usage:
    python deploy.py                    # Deploy committed build to phaser.local
    python deploy.py 192.168.1.100      # Deploy to a specific host
    python deploy.py --build            # Rebuild from source first, then deploy
    python deploy.py --build-only       # Rebuild, don't deploy
    python deploy.py --sim-only         # Prepare for --sim, don't deploy
    python deploy.py --radar            # Include the CW radar app

Building is opt-in via --build (needs Node + npm). If the committed build
is missing entirely, deploy.py builds it automatically when npm is
available, and tells you what to do when it isn't.

The CW radar frontend is opt-in via --radar: it is a separate app on :8081
with no simulation path, so most runs do not need it. --no-radar is still
accepted, as a no-op.
"""

import shutil
import subprocess
import sys
import os
from pathlib import Path

# Default Pi settings
DEFAULT_HOST = "phaser.local"
DEFAULT_USER = "analog"
REMOTE_DIR = "/home/analog/pyadi-iio/examples/phaser"
REMOTE_WWW = f"{REMOTE_DIR}/frontend/dist"
REMOTE_RADAR_WWW = f"{REMOTE_DIR}/frontend-radar/dist"

def run(cmd, cwd=None, check=True):
    """Run a command and print it."""
    print(f"  $ {cmd}")
    result = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=False)
    if check and result.returncode != 0:
        print(f"  ERROR: Command failed with exit code {result.returncode}")
        sys.exit(1)
    return result


def have_npm():
    """True if npm is on PATH (npm.cmd on Windows)."""
    return shutil.which("npm") is not None or shutil.which("npm.cmd") is not None


def ensure_deps(pkg_dir, label):
    """Install npm deps if node_modules is absent.

    vite is a devDependency, so a fresh clone has no build tooling at all and
    `npm run build` dies with a bare "vite: not found".
    """
    if (pkg_dir / "node_modules").exists():
        return
    print(f"  node_modules missing for {label}; installing...")
    run("npm install", cwd=pkg_dir)


def build(pkg_dir, dist_dir, label):
    """Build one frontend, installing deps first if needed."""
    if not (pkg_dir / "package.json").exists():
        print(f"  ERROR: {pkg_dir.name}/package.json not found")
        sys.exit(1)
    ensure_deps(pkg_dir, label)
    run("npm run build", cwd=pkg_dir)
    if not (dist_dir / "index.html").exists():
        print(f"  ERROR: {label} build failed - {dist_dir.name}/index.html not found")
        sys.exit(1)
    print(f"  OK: {label} built")


def staleness_warning(pkg_dir, dist_dir, label):
    """Warn if sources look newer than the committed build.

    Only advisory: mtimes are unreliable across clones and OneDrive sync,
    so this never blocks a deploy — it just stops a stale dist/ from
    shipping silently, which is the whole risk of committing build output.
    """
    stamp = dist_dir / "index.html"
    if not stamp.exists():
        return
    built = stamp.stat().st_mtime
    watched = []
    src = pkg_dir / "src"
    if src.exists():
        watched += [p for p in src.rglob("*") if p.is_file()]
    for extra in ("index.html", "package.json", "vite.config.js"):
        p = pkg_dir / extra
        if p.exists():
            watched.append(p)
    newer = [p for p in watched if p.stat().st_mtime > built + 1]
    if newer:
        print(f"  WARN: {len(newer)} source file(s) newer than the committed "
              f"{label} build, e.g. {newer[0].relative_to(pkg_dir)}")
        print("        Deploying the committed build anyway. Use --build to rebuild.")


def main():
    script_dir = Path(__file__).parent.resolve()
    frontend_dir = script_dir / "frontend"
    dist_dir = frontend_dir / "dist"
    radar_dir = script_dir / "frontend-radar"
    radar_dist_dir = radar_dir / "dist"

    # Parse args
    known_flags = {"--build", "--build-only", "--sim-only", "--radar", "--no-radar"}
    unknown = [a for a in sys.argv[1:]
               if a.startswith("--") and a not in known_flags]
    if unknown:
        print(f"  ERROR: unknown option(s): {' '.join(unknown)}")
        print(f"  Known options: {' '.join(sorted(known_flags))}")
        sys.exit(1)

    sim_only = "--sim-only" in sys.argv
    build_only = "--build-only" in sys.argv
    # Building is opt-in now that dist/ is committed. --build-only implies it.
    want_build = "--build" in sys.argv or build_only
    # Radar is opt-in. It is a separate frontend served on :8081 with no
    # simulation path, so building it by default was wasted work on nearly
    # every run. "--no-radar" is still accepted, but is now redundant.
    want_radar = "--radar" in sys.argv and not sim_only
    host = DEFAULT_HOST
    for arg in sys.argv[1:]:
        if not arg.startswith("--"):
            host = arg
            break

    radar_present = want_radar and (radar_dir / "package.json").exists()

    print("=" * 60)
    print("  Phaser Deployment")
    if sim_only:
        print("  (local sim prep only)")
    elif radar_present:
        print("  (beamforming app + radar app)")
    print("=" * 60)

    # Step 1: Frontend build (opt-in), or verify the committed build
    targets = [(frontend_dir, dist_dir, "Beamforming frontend")]
    if radar_present:
        targets.append((radar_dir, radar_dist_dir, "Radar frontend"))
    if want_radar and not radar_present:
        print("  WARN: --radar given but frontend-radar/package.json not found")

    missing = [t for t in targets if not (t[1] / "index.html").exists()]
    if missing and not want_build:
        names = ", ".join(t[2] for t in missing)
        if have_npm():
            print(f"\n[1/4] No committed build found for: {names}")
            print("      Building it now (npm is available).")
            want_build = True
        else:
            print(f"\n  ERROR: no committed build found for: {names}")
            print("  The built frontend is normally committed by CI. Either:")
            print("    - pull/checkout a commit that has it, or")
            print("    - install Node + npm and re-run with --build")
            sys.exit(1)

    if want_build:
        print("\n[1/4] Building frontend(s)...")
        if not have_npm():
            print("  ERROR: --build requires Node + npm, which are not on PATH.")
            print("  Drop --build to deploy the committed build instead.")
            sys.exit(1)
        for pkg_dir, dst, label in targets:
            build(pkg_dir, dst, label)
    else:
        print("\n[1/4] Using committed build (no build step)...")
        for pkg_dir, dst, label in targets:
            print(f"  OK: {label} present")
            staleness_warning(pkg_dir, dst, label)

    if sim_only:
        print("\n" + "=" * 60)
        print("  Ready for simulation. Nothing was deployed.")
        print("  Run the backend against simulated hardware with:")
        if os.name == "nt":
            print('    $env:PYTHONIOENCODING = "utf-8"')
        print("    python phaser_headless.py --sim")
        print("  Then open http://localhost:8080")
        print("=" * 60)
        return

    if build_only:
        print("\n--build-only specified, skipping deployment.")
        return

    # Step 2: Copy backend Python files
    print(f"\n[2/4] Copying backend scripts to {host}...")
    backend_files = [
        "phaser_headless.py",
        "phaser_cal_headless.py",
        "phaser_find_hb100_headless.py",
        "phaser_cw_radar.py",
        # Helpers that phaser_headless.py imports at module top. The Pi
        # historically shipped its own copies; now that these live in the
        # repo they're deployed as one atomic set to prevent version skew.
        # NOTE: config.py is deliberately excluded — the Pi's copy may have
        # site-specific values (URIs, calibrated defaults) that we don't
        # want to overwrite. Use config_custom.py for local overrides.
        "ADAR_pyadi_functions.py",
        "SDR_functions.py",
        "phaser_functions.py",
    ]
    for filename in backend_files:
        py_file = script_dir / filename
        if py_file.exists():
            run(f'scp "{py_file}" {DEFAULT_USER}@{host}:{REMOTE_DIR}/')
            print(f"  OK: {filename} copied")
        else:
            print(f"  SKIP: {filename} not found")

    # Step 3: Copy frontend(s)
    print(f"\n[3/4] Copying frontend to {host}:{REMOTE_WWW}/...")
    run(f'ssh {DEFAULT_USER}@{host} "mkdir -p {REMOTE_WWW}"')
    run(f'scp -r "{dist_dir}"/* {DEFAULT_USER}@{host}:{REMOTE_WWW}/')
    print("  OK: Beamforming frontend copied")

    if radar_present:
        run(f'ssh {DEFAULT_USER}@{host} "mkdir -p {REMOTE_RADAR_WWW}"')
        run(f'scp -r "{radar_dist_dir}"/* {DEFAULT_USER}@{host}:{REMOTE_RADAR_WWW}/')
        print("  OK: Radar frontend copied")

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
    print(f"  Beamforming UI: http://{host}:8080")
    if radar_present:
        print(f"  Radar UI:       http://{host}:8081")
    print("=" * 60)

if __name__ == "__main__":
    main()
