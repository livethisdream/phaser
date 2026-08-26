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

Provisioning: deploy.py owns the systemd unit. If
/etc/systemd/system/phaser-headless.service is absent it renders
scripts/phaser-headless.service.template and installs it, so deploying to a
never-provisioned Pi produces a running, boot-enabled service rather than a
"Deployment complete!" with nothing behind it. It does NOT install the Pi's
Python dependencies -- that is scripts/setup.sh's job -- but it does check for
them and fails loudly when they are missing.
"""

import atexit
import shutil
import socket
import subprocess
import sys
import os
import tempfile
from pathlib import Path

# Default Pi settings
DEFAULT_HOST = "phaser.local"
DEFAULT_USER = "analog"
REMOTE_DIR = "/home/analog/pyadi-iio/examples/phaser"
REMOTE_WWW = f"{REMOTE_DIR}/frontend/dist"
REMOTE_RADAR_WWW = f"{REMOTE_DIR}/frontend-radar/dist"
# Parents of the two above. We scp the local dist/ directory *into* these rather
# than scp'ing "dist/*", because run() uses shell=True -- which is cmd.exe on
# Windows, and cmd.exe does not expand globs. The old form worked only because
# it had only ever been run from a Unix shell; from PowerShell scp received a
# literal "dist/*" and failed.
REMOTE_WWW_PARENT = f"{REMOTE_DIR}/frontend"
REMOTE_RADAR_WWW_PARENT = f"{REMOTE_DIR}/frontend-radar"

# systemd unit. The interpreter must match the one the unit's ExecStart uses,
# because the dependency probe below imports through it.
SERVICE_NAME = "phaser-headless"
UNIT_PATH = f"/etc/systemd/system/{SERVICE_NAME}.service"
REMOTE_PYTHON = "/usr/bin/python3"

# Imported at the top of phaser_headless.py; a missing one is a crash loop that
# deploy.py would otherwise never see. Module name -> pip name.
RUNTIME_DEPS = {"zmq": "pyzmq", "msgpack": "msgpack", "websockets": "websockets"}

def run(cmd, cwd=None, check=True):
    """Run a command and print it."""
    print(f"  $ {cmd}")
    result = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=False)
    if check and result.returncode != 0:
        print(f"  ERROR: Command failed with exit code {result.returncode}")
        sys.exit(1)
    return result


# Path to the shared ssh control socket for this run, or None if multiplexing
# is unavailable. See start_mux().
_MUX_PATH = None


def mux_opts():
    """ssh/scp options that reuse this run's shared connection, as a list."""
    return [] if _MUX_PATH is None else ["-o", f"ControlPath={_MUX_PATH}"]


def mux_opts_str():
    """Same, for the shell-string commands passed to run()."""
    return "" if _MUX_PATH is None else f" -o ControlPath={_MUX_PATH}"


def start_mux(host):
    """Authenticate once and share that connection with every later ssh/scp.

    Without this, a Pi reached by password prompts for it on the mkdir, on each
    of the ten-odd scp calls, and again on every probe -- unusable in practice.
    With it there is exactly one prompt. It is a straight speed win for
    key-based runs too, which otherwise pay a full TCP + handshake per file.

    Best effort: if the master will not start, every command still works
    standalone, just with the prompting (or the handshakes) it had before.
    """
    global _MUX_PATH
    if os.name == "nt":
        # Windows' OpenSSH port has never supported ControlMaster.
        return
    path = os.path.join(tempfile.mkdtemp(prefix="phaser-mux-"), "cm")
    print("  Opening a shared ssh connection (one auth for the whole deploy)...")
    result = subprocess.run(
        ["ssh", "-o", "ControlMaster=yes", "-o", f"ControlPath={path}",
         "-o", "ControlPersist=300", "-N", "-f", f"{DEFAULT_USER}@{host}"]
    )
    if result.returncode != 0:
        print("  NOTE: shared connection unavailable; each step authenticates on its own.")
        return
    _MUX_PATH = path
    atexit.register(stop_mux, host)


def stop_mux(host):
    """Tear the shared connection down. Registered with atexit so the sys.exit
    paths below cannot leave a background ssh and a stray socket behind."""
    global _MUX_PATH
    if _MUX_PATH is None:
        return
    path, _MUX_PATH = _MUX_PATH, None
    subprocess.run(["ssh", "-O", "exit", "-o", f"ControlPath={path}",
                    f"{DEFAULT_USER}@{host}"], capture_output=True)
    shutil.rmtree(os.path.dirname(path), ignore_errors=True)


def ssh_probe(host, remote_cmd, batch=True):
    """Run a command on the Pi and return its exit code, output suppressed.

    Deliberately not `run()`: these are yes/no questions about the target, so a
    non-zero exit is an answer rather than a failure worth printing.

    batch=False drops BatchMode so ssh may prompt for a password. Needed for a
    Pi reached by password rather than by key -- under BatchMode every probe
    fails identically to "not installed", which is a lie about the target.
    """
    cmd = ["ssh"] + mux_opts()
    if batch:
        cmd += ["-o", "BatchMode=yes"]
    cmd += ["-o", "ConnectTimeout=10", f"{DEFAULT_USER}@{host}", remote_cmd]
    # Capture only under BatchMode. Without it ssh may need to show a password
    # prompt, and a captured prompt is an invisible hang.
    return subprocess.run(cmd, capture_output=batch, text=True).returncode


def render_unit(script_dir):
    """Render the systemd unit from the template, or None if it's missing.

    Substitution is from this module's constants on purpose -- it is what keeps
    the unit's WorkingDirectory identical to the directory we scp into.
    """
    tpl = script_dir / "scripts" / f"{SERVICE_NAME}.service.template"
    if not tpl.exists():
        return None
    return (tpl.read_text(encoding="utf-8")
            .replace("@USER@", DEFAULT_USER)
            .replace("@INSTALL_DIR@", REMOTE_DIR)
            .replace("@PYTHON@", REMOTE_PYTHON))


def install_unit(host, script_dir, state):
    """Install, enable and start the systemd unit on a Pi that has none.

    Returns True when it started the service, so the caller can skip step 5.

    scp-then-`sudo install` rather than piping a heredoc through `ssh -t`: with
    a tty allocated, stdin belongs to the sudo password prompt, so redirecting
    the unit text into it is not available to us.
    """
    unit_text = render_unit(script_dir)
    if unit_text is None:
        print(f"  ERROR: scripts/{SERVICE_NAME}.service.template not found.")
        print("  Cannot provision the service. Check out a complete tree, or")
        print(f"  run ./scripts/setup.sh {host} from a full checkout.")
        sys.exit(1)

    # Staged in a temp dir, not the repo: the rendered unit is a build artifact,
    # and phaser_headless.py's static server falls back to serving the install
    # dir itself when no frontend is found -- so nothing stray should linger in
    # either tree.
    tmp = Path(tempfile.mkdtemp(prefix="phaser-deploy-"))
    staged = tmp / f"{SERVICE_NAME}.service"
    remote_staged = f"{REMOTE_DIR}/{SERVICE_NAME}.service"
    try:
        staged.write_text(unit_text, encoding="utf-8")
        run(f'scp{mux_opts_str()} "{staged}" {DEFAULT_USER}@{host}:{remote_staged}')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("  NOTE: You may need to enter the sudo password on the Pi")
    # enable, not just start: a unit installed here must survive a reboot, and
    # the enable that setup-pi.sh used to do no longer exists.
    #
    # The restart rides along in this same chain rather than waiting for step 5.
    # sudo's credential timestamp is per-tty, and each `ssh -t` gets a fresh
    # pty, so a separate restart is a second password prompt for no reason.
    # One ssh session, one pty, one prompt.
    result = run(
        f'ssh -t{mux_opts_str()} {DEFAULT_USER}@{host} "'
        f'sudo install -m 644 {remote_staged} {UNIT_PATH} && '
        f'sudo systemctl daemon-reload && '
        f'sudo systemctl enable {SERVICE_NAME} && '
        f'sudo systemctl restart {SERVICE_NAME}"',
        check=False,
    )
    # Unconditionally, and outside the sudo chain: when the chain failed it
    # never reached its own cleanup, stranding the staged unit in the install
    # dir. Needs no sudo -- the file belongs to DEFAULT_USER.
    ssh_probe(host, f"rm -f {remote_staged}")

    if result.returncode != 0:
        print(f"  ERROR: could not install and start {SERVICE_NAME} "
              f"(exit {result.returncode}).")
        if not state.get("sudo_nopasswd"):
            # The overwhelmingly likely cause when running non-interactively:
            # `sudo` wants a password and there is no terminal to read it from.
            print("  sudo on this Pi requires a password, and this run has no")
            print("  terminal to type it into. Re-run from an interactive shell:")
            print(f"    python deploy.py {host}")
        else:
            print(f"  The service cannot start without it. "
                  f"Try ./scripts/setup.sh {host}")
        sys.exit(1)
    print(f"  OK: {SERVICE_NAME}.service installed, enabled and started")
    return True


def tcp_reachable(host, port=22, timeout=10):
    """(ok, error) for a plain TCP connect to the Pi's ssh port.

    Separating this from authentication is the whole point: a refused or timed
    out connection is a network problem, while a completed connection that ssh
    then rejects is a credentials problem. Collapsing the two sends you off to
    fix the wrong thing.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, None
    except OSError as exc:
        return False, exc


def check_reachable(host):
    """Verify the Pi is there. Returns True if key-based (non-interactive) ssh works.

    Returning rather than exiting on a password-only Pi is deliberate. scp and
    ssh prompt for a password perfectly well in an interactive shell, and that
    is how this Pi was reached long before any of these checks existed --
    refusing to deploy without a key would break a working setup.
    """
    ok, exc = tcp_reachable(host)
    if not ok:
        print(f"  ERROR: cannot reach {host} on port 22: {exc}")
        print("  The Pi is off, on another network, or the name does not resolve.")
        print(f"  Check:  ssh {DEFAULT_USER}@{host} 'echo ok'")
        sys.exit(1)

    if ssh_probe(host, "true") == 0:
        print(f"  OK: key-based ssh to {DEFAULT_USER}@{host} works")
        return True

    print(f"  NOTE: {host} is reachable, but your key is not authorized on it.")
    print("        ssh and scp will prompt for the Pi's password -- fine when you")
    print("        are running this by hand, impossible from an unattended run.")
    print(f"        To stop the prompting:  ssh-copy-id {DEFAULT_USER}@{host}")
    return False


def probe_pi(host, batch):
    """Collect the Pi's provisioning state in ONE round trip, or None on failure.

    One ssh call rather than five: with key auth it is simply faster, and
    without one it is the difference between a single password prompt and a
    fistful of them.
    """
    mods = " ".join(RUNTIME_DEPS)
    script = (
        f'test -e {UNIT_PATH} && echo unit=1 || echo unit=0; '
        f'test -e {REMOTE_DIR}/config.py && echo config=1 || echo config=0; '
        f'for m in {mods}; do {REMOTE_PYTHON} -c "import $m" 2>/dev/null '
        f'&& echo "dep_$m=1" || echo "dep_$m=0"; done; '
        f'sudo -n true 2>/dev/null && echo sudo_nopasswd=1 || echo sudo_nopasswd=0'
    )
    cmd = ["ssh"] + mux_opts()
    if batch:
        cmd += ["-o", "BatchMode=yes"]
    cmd += ["-o", "ConnectTimeout=10", f"{DEFAULT_USER}@{host}", script]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    state = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, _, value = line.strip().partition("=")
            state[key] = value == "1"
    return state or None


def check_runtime_deps(host, state):
    """Fail loudly if the Pi lacks what phaser_headless.py imports.

    Probed through REMOTE_PYTHON as DEFAULT_USER -- the exact interpreter and
    user the unit runs as -- so a pass here means the import will pass there.
    """
    missing = [pip_name for mod, pip_name in RUNTIME_DEPS.items()
               if not state.get(f"dep_{mod}", False)]
    if not missing:
        print("  OK: Python dependencies present")
        return
    print(f"  ERROR: {host} is missing Python package(s): {' '.join(missing)}")
    print("  phaser_headless.py imports these at startup, so the service would")
    print("  crash-loop instead of serving. Install them on the Pi with:")
    print(f"    ssh {DEFAULT_USER}@{host} '{REMOTE_PYTHON} -m pip install --user "
          f"{' '.join(missing)}'")
    print(f"  or run ./scripts/setup.sh {host}, which does it for you.")
    sys.exit(1)


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


def finish(host, radar_present):
    """The closing banner. Shared, because a fresh-Pi deploy reaches the end
    through the install chain in step 4 rather than through step 5's restart."""
    print("\n" + "=" * 60)
    print("  Deployment complete!")
    print(f"  Beamforming UI: http://{host}:8080")
    if radar_present:
        print(f"  Radar UI:       http://{host}:8081")
    print("=" * 60)


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
            print(f"\n[1/5] No committed build found for: {names}")
            print("      Building it now (npm is available).")
            want_build = True
        else:
            print(f"\n  ERROR: no committed build found for: {names}")
            print("  The built frontend is normally committed by CI. Either:")
            print("    - pull/checkout a commit that has it, or")
            print("    - install Node + npm and re-run with --build")
            sys.exit(1)

    if want_build:
        print("\n[1/5] Building frontend(s)...")
        if not have_npm():
            print("  ERROR: --build requires Node + npm, which are not on PATH.")
            print("  Drop --build to deploy the committed build instead.")
            sys.exit(1)
        for pkg_dir, dst, label in targets:
            build(pkg_dir, dst, label)
    else:
        print("\n[1/5] Using committed build (no build step)...")
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
    print(f"\n[2/5] Copying backend scripts to {host}...")
    key_auth = check_reachable(host)
    start_mux(host)
    # One round trip for everything steps 2 and 4 need to know about the Pi.
    state = probe_pi(host, batch=key_auth)
    if state is None:
        print("  WARN: could not read the Pi's provisioning state.")
        print("        Deploying anyway; the checks below are skipped.")
    # A never-provisioned Pi has no install dir at all, and scp will not create
    # one -- every copy below would fail on a path that does not exist.
    run(f'ssh{mux_opts_str()} {DEFAULT_USER}@{host} "mkdir -p {REMOTE_DIR}"')
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
        # AD9361 filter configs. phaser_find_hb100_headless.py does
        # `my_sdr.filter = "LTE20_MHz.ftr"`, which pyadi-iio resolves relative
        # to the process CWD — i.e. REMOTE_DIR, per the systemd unit's
        # WorkingDirectory. It used to resolve only because pyadi-iio's own
        # examples/phaser directory happens to ship these; that made the
        # deploy depend on what was already on the target. The load is wrapped
        # in a try/except that merely warns, so a missing file degrades to an
        # unfiltered wideband HB100 search instead of failing loudly.
        "LTE5_MHz.ftr",
        "LTE10_MHz.ftr",
        "LTE20_MHz.ftr",
    ]
    for filename in backend_files:
        py_file = script_dir / filename
        if py_file.exists():
            run(f'scp{mux_opts_str()} "{py_file}" {DEFAULT_USER}@{host}:{REMOTE_DIR}/')
            print(f"  OK: {filename} copied")
        else:
            print(f"  SKIP: {filename} not found")

    # config.py is excluded from backend_files because the Pi's copy may hold
    # site-specific values. But "don't overwrite" is not "never create": a Pi
    # provisioned from scratch has an empty install dir, and phaser_headless.py
    # does `import config` -> sys.exit(1) at module level, so with no config.py
    # the unit crash-loops while deploy.py reports success. Seed it only when
    # the remote has none, which leaves the redeploy case untouched.
    if state is None:
        # Unknown state: the safe assumption is that the Pi has its own config,
        # because seeding over a real one would clobber site-specific values
        # while merely failing to seed leaves an obvious import error.
        print("  SKIP: config.py (state unknown; not risking the Pi's copy)")
    elif state.get("config"):
        print("  SKIP: config.py (the Pi's own copy is kept)")
    else:
        run(f'scp{mux_opts_str()} "{script_dir / "config.py"}" {DEFAULT_USER}@{host}:{REMOTE_DIR}/')
        print("  OK: config.py seeded (none on the Pi); edit it there for "
              "site-specific values")

    # Step 3: Copy frontend(s)
    print(f"\n[3/5] Copying frontend to {host}:{REMOTE_WWW}/...")
    run(f'ssh{mux_opts_str()} {DEFAULT_USER}@{host} "mkdir -p {REMOTE_WWW_PARENT}"')
    run(f'scp -r{mux_opts_str()} "{dist_dir}" {DEFAULT_USER}@{host}:{REMOTE_WWW_PARENT}/')
    print("  OK: Beamforming frontend copied")

    if radar_present:
        run(f'ssh{mux_opts_str()} {DEFAULT_USER}@{host} "mkdir -p {REMOTE_RADAR_WWW_PARENT}"')
        run(f'scp -r{mux_opts_str()} "{radar_dist_dir}" {DEFAULT_USER}@{host}:{REMOTE_RADAR_WWW_PARENT}/')
        print("  OK: Radar frontend copied")

    # Step 4: Provisioning check
    print(f"\n[4/5] Checking Pi provisioning...")
    restarted = False
    if state is None:
        print("  SKIP: provisioning unverified (could not probe the Pi).")
        print(f"        If the restart below fails, check that {UNIT_PATH}")
        print("        exists and that the Python dependencies are installed.")
    else:
        # Deps first: it fails a doomed deploy before prompting for a sudo
        # password to install a unit that would only crash-loop anyway.
        check_runtime_deps(host, state)
        if state.get("unit"):
            print(f"  OK: {SERVICE_NAME}.service already installed")
        else:
            print(f"  {UNIT_PATH} not found; installing it.")
            restarted = install_unit(host, script_dir, state)

    # Step 5: Restart service
    print(f"\n[5/5] Restarting {SERVICE_NAME} service...")
    if restarted:
        # Already done, in the install chain above, on purpose -- see install_unit.
        print("  OK: started as part of the install above")
        finish(host, radar_present)
        return
    print("  NOTE: You may need to enter the sudo password on the Pi")
    result = run(
        f'ssh -t{mux_opts_str()} {DEFAULT_USER}@{host} "sudo systemctl restart {SERVICE_NAME}"',
        check=False
    )
    if result.returncode != 0:
        # The unit exists by now -- we just installed or confirmed it -- so a
        # failure here is real. Exiting non-zero rather than warning: printing
        # "Deployment complete!" over a service that never started is how a
        # fresh Pi looked deployed for as long as it did.
        print(f"  ERROR: could not restart {SERVICE_NAME} (exit {result.returncode}).")
        # Same diagnosis as install_unit: the common non-interactive failure is
        # sudo wanting a password with no terminal to read it from, which has
        # nothing to do with the service and sends you to the wrong logs.
        if state is not None and not state.get("sudo_nopasswd"):
            print("  sudo on this Pi requires a password. If you ran this")
            print("  non-interactively, re-run from a terminal:")
            print(f"    python deploy.py {host}")
        print(f"  Otherwise check the Pi:  ssh {DEFAULT_USER}@{host} "
              f"'sudo journalctl -u {SERVICE_NAME} -n 50'")
        sys.exit(1)
    print("  OK: Service restarted")
    finish(host, radar_present)


if __name__ == "__main__":
    main()
