#!/usr/bin/env python3
"""
Deploy the Phaser browser UI to the Raspberry Pi.

    python deploy.py                    # deploy to phaser.local
    python deploy.py 192.168.1.100      # a specific host
    python deploy.py --build            # rebuild the frontend first (needs Node)
    python deploy.py --sim-only         # prepare for --sim, don't deploy
    python deploy.py --radar            # also deploy the CW radar app
    python deploy.py --yes              # never prompt (unattended)
    python deploy.py --no-deps          # never install packages on the Pi

This is the ONLY command. It provisions a never-touched Pi and redeploys to a
working one by the same path: it installs missing Python packages (asking
first), installs or UPDATES the systemd unit, and restarts the service.

The built frontends are committed to the repo (by CI, see
.github/workflows/build-frontends.yml), so a fresh clone can deploy with no
Node installed. Building is opt-in via --build.

No shell, anywhere. Every local command is an argv list, so cmd.exe never sees
it on Windows; every remote command is an argv list joined exactly once by
phaser_deploy.remote.ssh_argv, so the Pi's /bin/sh never sees an interpolated
path. That is deliberate: globbing, quoting, %VAR% expansion and && handling
all differ between cmd.exe and /bin/sh, and every deployment bug this tool has
had came from letting one of them interpret a string we built.
"""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))

from phaser_deploy import advice as _advice
from phaser_deploy.local import (
    Finding, Level, check_local, check_network, find_exe, npm_argv, report,
)
from phaser_deploy.remote import (
    SCP_BASE, SshSession, Target, scp_argv, ssh_argv,
)

DEFAULT_HOST = "phaser.local"
DEFAULT_USER = "analog"
REMOTE_DIR = "/home/analog/pyadi-iio/examples/phaser"
REMOTE_WWW_PARENT = f"{REMOTE_DIR}/frontend"
REMOTE_RADAR_WWW_PARENT = f"{REMOTE_DIR}/frontend-radar"

SERVICE_NAME = "phaser-headless"
UNIT_PATH = f"/etc/systemd/system/{SERVICE_NAME}.service"
REMOTE_PYTHON = "/usr/bin/python3"

# Imported at the top of phaser_headless.py. Module name -> pip name.
RUNTIME_DEPS = {"zmq": "pyzmq", "msgpack": "msgpack", "websockets": "websockets"}

BACKEND_FILES = [
    "phaser_headless.py",
    "phaser_cal_headless.py",
    "phaser_find_hb100_headless.py",
    "phaser_cw_radar.py",
    # Helpers phaser_headless.py imports at module top. Deployed as one atomic
    # set to prevent version skew with the Pi's own older copies.
    "ADAR_pyadi_functions.py",
    "SDR_functions.py",
    "phaser_functions.py",
    # AD9361 filter configs. phaser_find_hb100_headless.py loads these by bare
    # filename, which pyadi-iio resolves against the process CWD -- i.e. the
    # unit's WorkingDirectory.
    "LTE5_MHz.ftr",
    "LTE10_MHz.ftr",
    "LTE20_MHz.ftr",
]

# Sentinels around the probe's output. Without them, parsing accepted any line
# containing "=", so an MOTD or a login banner could inject state.
BEGIN, END = "__PHASER_BEGIN__", "__PHASER_END__"


def echo(argv):
    """Show a command the way a person would type it, without running a shell."""
    import shlex
    print("  $ " + shlex.join(str(a) for a in argv), flush=True)


def run(argv, cwd=None, check=True):
    """Run a local command. Never shell=True."""
    echo(argv)
    result = subprocess.run(argv, cwd=cwd)
    if check and result.returncode != 0:
        print(f"  ERROR: command failed with exit code {result.returncode}")
        sys.exit(1)
    return result


PROBE = f"""
echo {BEGIN}
if [ -e {UNIT_PATH} ]; then
  echo unit_sha=$(sha256sum {UNIT_PATH} | cut -d" " -f1)
else
  echo unit_sha=
fi
[ -e {REMOTE_DIR}/config.py ] && echo config=1 || echo config=0
for m in {" ".join(RUNTIME_DEPS)}; do
  {REMOTE_PYTHON} -c "import $m" 2>/dev/null && echo dep_$m=1 || echo dep_$m=0
done
sudo -n true 2>/dev/null && echo sudo_nopasswd=1 || echo sudo_nopasswd=0
command -v tar >/dev/null 2>&1 && echo tar=1 || echo tar=0
echo {END}
"""


def probe(target, session, *, batch):
    """One round trip for everything we need to know about the Pi."""
    argv = ssh_argv(target, ["sh", "-c", PROBE], batch=batch,
                    mux=session.path, accept_new=True)
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    lines = result.stdout.splitlines()
    if BEGIN not in lines or END not in lines:
        return None
    body = lines[lines.index(BEGIN) + 1:lines.index(END)]
    state = {}
    for line in body:
        key, _, value = line.strip().partition("=")
        state[key] = value
    return state


def key_auth_works(target, session):
    """True if ssh can authenticate without a password."""
    argv = ssh_argv(target, ["true"], batch=True, mux=session.path,
                    accept_new=True, timeout=10)
    return subprocess.run(argv, capture_output=True).returncode == 0


def render_unit(script_dir):
    """Render the unit template from this module's constants, or None."""
    tpl = script_dir / "scripts" / f"{SERVICE_NAME}.service.template"
    if not tpl.exists():
        return None
    return (tpl.read_text(encoding="utf-8")
            .replace("@USER@", DEFAULT_USER)
            .replace("@INSTALL_DIR@", REMOTE_DIR)
            .replace("@PYTHON@", REMOTE_PYTHON))


def unit_sha(text):
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sync_unit(target, session, script_dir, state, *, interactive):
    """Install the unit, or update it when the Pi's copy has drifted.

    Checking presence alone was not enough: a Pi provisioned by an older
    version keeps its old unit forever, so every template change silently never
    reaches the machines already in the field. Compare content, not existence.

    Returns "unchanged", or "installed"/"updated" (both of which also restart).
    """
    text = render_unit(script_dir)
    if text is None:
        print(f"  ERROR: scripts/{SERVICE_NAME}.service.template not found.")
        sys.exit(1)

    want = unit_sha(text)
    have = (state or {}).get("unit_sha", "")
    if have == want:
        print(f"  OK: {SERVICE_NAME}.service is current")
        return "unchanged"

    verb = "updated" if have else "installed"
    if have:
        print(f"  {SERVICE_NAME}.service on the Pi differs from the template; updating.")
    else:
        print(f"  {UNIT_PATH} not found; installing it.")

    tmp = Path(tempfile.mkdtemp(prefix="phaser-unit-"))
    staged = tmp / f"{SERVICE_NAME}.service"
    remote_staged = f"{REMOTE_DIR}/{SERVICE_NAME}.service"
    try:
        staged.write_text(text, encoding="utf-8")
        run(scp_argv([staged], f"{target}:{remote_staged}", mux=session.path))
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    # install + reload + enable + restart in ONE ssh session: sudo's credential
    # timestamp is per-tty and each ssh -t gets a fresh pty, so splitting these
    # would prompt for the password more than once.
    chain = [
        "sudo", "install", "-m", "644", remote_staged, UNIT_PATH, "&&",
        "sudo", "systemctl", "daemon-reload", "&&",
        "sudo", "systemctl", "enable", SERVICE_NAME, "&&",
        "sudo", "systemctl", "restart", SERVICE_NAME,
    ]
    # "&&" must reach the Pi's shell as an operator, so the chain is a script.
    import shlex
    script = " ".join(shlex.quote(a) if a != "&&" else "&&" for a in chain)
    if interactive:
        print("  NOTE: you may need to enter the sudo password on the Pi")
    result = subprocess.run(
        ssh_argv(target, ["sh", "-c", script], tty=interactive, mux=session.path))

    # Always, and outside the chain: a failed chain never reaches its own
    # cleanup, stranding the staged unit where the static server may serve it.
    subprocess.run(ssh_argv(target, ["rm", "-f", remote_staged],
                            batch=True, mux=session.path), capture_output=True)

    if result.returncode != 0:
        print(f"  ERROR: could not install {SERVICE_NAME} (exit {result.returncode}).")
        if not (state or {}).get("sudo_nopasswd") == "1":
            print("  " + _advice.advice("needs_terminal", host=target.host))
        sys.exit(1)
    print(f"  OK: {SERVICE_NAME}.service {verb}, enabled and started")
    return verb


def dep_findings(target, state, *, install_ok, interactive, assume_yes):
    """Decide what to do about missing Pi packages. Returns (findings, to_install)."""
    missing = [pip for mod, pip in RUNTIME_DEPS.items()
               if (state or {}).get(f"dep_{mod}") != "1"]
    if not missing:
        return [], []
    remedy = _advice.advice("missing_deps", user=target.user, host=target.host,
                            python=REMOTE_PYTHON, pkgs=" ".join(missing))
    msg = (f"{target.host} is missing Python package(s): {' '.join(missing)} -- "
           "phaser_headless.py imports these at startup, so the service would "
           "crash-loop instead of serving")
    if not install_ok:
        return [Finding(Level.BLOCK, "deps", msg, remedy)], []
    if assume_yes:
        print(f"  Installing missing package(s) on the Pi: {' '.join(missing)}")
        return [], missing
    if not interactive:
        # input() on a closed stdin raises EOFError and looks like a hang in CI.
        return [Finding(Level.BLOCK, "deps", msg + " (no terminal to ask on; "
                        "pass --yes to install automatically)", remedy)], []
    print(f"  {msg}.")
    answer = input(f"  Install {' '.join(missing)} on {target.host} now? [Y/n] ").strip().lower()
    if answer in ("", "y", "yes"):
        return [], missing
    return [Finding(Level.BLOCK, "deps", "declined to install", remedy)], []


def install_deps(target, session, packages):
    """pip install --user on the Pi. No sudo: it writes to ~/.local as analog."""
    argv = ssh_argv(target,
                    [REMOTE_PYTHON, "-m", "pip", "install", "--user", *packages],
                    mux=session.path)
    result = run(argv, check=False)
    if result.returncode != 0:
        print(f"  ERROR: installing {' '.join(packages)} failed.")
        sys.exit(1)
    print(f"  OK: installed {' '.join(packages)}")


def build(pkg_dir, dist_dir, label):
    """Build one frontend, installing node deps first if needed."""
    if not (pkg_dir / "package.json").exists():
        print(f"  ERROR: {pkg_dir.name}/package.json not found")
        sys.exit(1)
    if not (pkg_dir / "node_modules").exists():
        print(f"  node_modules missing for {label}; installing...")
        run(npm_argv("install"), cwd=pkg_dir)
    run(npm_argv("run", "build"), cwd=pkg_dir)
    if not (dist_dir / "index.html").exists():
        print(f"  ERROR: {label} build failed - {dist_dir.name}/index.html not found")
        sys.exit(1)
    print(f"  OK: {label} built")


def staleness_warning(pkg_dir, dist_dir, label):
    """Advisory only: mtimes are unreliable across clones, so never block."""
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


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="deploy.py",
        description="Deploy the Phaser browser UI to a Raspberry Pi.")
    p.add_argument("host", nargs="?", default=DEFAULT_HOST,
                   help=f"Pi hostname or IP (default: {DEFAULT_HOST})")
    p.add_argument("--build", action="store_true", help="rebuild the frontend first")
    p.add_argument("--build-only", action="store_true", help="rebuild, don't deploy")
    p.add_argument("--sim-only", action="store_true", help="prepare for --sim, don't deploy")
    p.add_argument("--radar", action="store_true", help="also deploy the CW radar app")
    p.add_argument("--no-radar", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--yes", "-y", action="store_true", help="never prompt")
    p.add_argument("--no-deps", action="store_true",
                   help="never install packages on the Pi")
    # People arrive from setup.ps1 and type PowerShell-style switches.
    for ps in ("-Build", "-SkipPi", "-Yes"):
        p.add_argument(ps, action="store_true", help=argparse.SUPPRESS)
    return p.parse_args(argv)


def finish(host, radar_present):
    print("\n" + "=" * 60)
    print("  Deployment complete!")
    print(f"  Beamforming UI: http://{host}:8080")
    if radar_present:
        print(f"  Radar UI:       http://{host}:8081")
    print("=" * 60)


def main(argv=None):
    # A legacy Windows console is cp1252; a stray non-ASCII byte would raise
    # UnicodeEncodeError mid-deploy. line_buffering keeps our echoes in order
    # with the child processes' output when stdout is not a tty.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except (AttributeError, ValueError):
        pass

    args = parse_args(argv)
    script_dir = Path(__file__).parent.resolve()
    frontend_dir = script_dir / "frontend"
    dist_dir = frontend_dir / "dist"
    radar_dir = script_dir / "frontend-radar"
    radar_dist_dir = radar_dir / "dist"

    target = Target(DEFAULT_USER, args.host)
    interactive = sys.stdin.isatty()
    want_build = args.build or args.Build or args.build_only
    want_radar = args.radar and not args.sim_only
    radar_present = want_radar and (radar_dir / "package.json").exists()
    deploying = not (args.sim_only or args.build_only)

    print("=" * 60)
    print("  Phaser Deployment")
    print("=" * 60)

    # ---- Tier 1: local -------------------------------------------------------
    print("\n[1/5] Checking this machine...")
    findings = check_local(script_dir, want_build=want_build, need_ssh=deploying)
    targets = [(frontend_dir, dist_dir, "Beamforming frontend")]
    if radar_present:
        targets.append((radar_dir, radar_dist_dir, "Radar frontend"))
    missing_builds = [t for t in targets if not (t[1] / "index.html").exists()]
    if missing_builds and not want_build:
        if npm_argv() is not None:
            print(f"  No committed build for: {', '.join(t[2] for t in missing_builds)}")
            print("  Building it now (npm is available).")
            want_build = True
        else:
            findings.append(Finding(
                Level.BLOCK, "no_build",
                f"no committed build for: {', '.join(t[2] for t in missing_builds)}",
                "Check out a commit that has frontend/dist/, or install Node and "
                "re-run with --build."))
    if not report(findings):
        sys.exit(1)
    print("  OK: local prerequisites satisfied")

    # ---- Frontend ------------------------------------------------------------
    print("\n[2/5] Frontend...")
    if want_build:
        for pkg_dir, dst, label in targets:
            build(pkg_dir, dst, label)
    else:
        for pkg_dir, dst, label in targets:
            print(f"  OK: {label} present")
            staleness_warning(pkg_dir, dst, label)

    if args.sim_only:
        print("\n" + "=" * 60)
        print("  Ready for simulation. Nothing was deployed.")
        if os.name == "nt":
            print('    $env:PYTHONIOENCODING = "utf-8"')
        print("    python phaser_headless.py --sim")
        print("  Then open http://localhost:8080")
        print("=" * 60)
        return 0
    if args.build_only:
        print("\n--build-only specified, skipping deployment.")
        return 0

    # ---- Tier 2: network -----------------------------------------------------
    print(f"\n[3/5] Reaching {target}...")
    findings = check_network(target)
    if not report(findings):
        sys.exit(1)

    with SshSession(target) as session:
        have_key = key_auth_works(target, session)
        if have_key:
            print("  OK: key-based ssh works")
        else:
            print("  NOTE: your key is not authorized on this Pi; ssh will ask "
                  "for its password.")
            for line in _advice.advice("no_key", user=target.user,
                                       host=target.host).splitlines():
                print("    " + line)

        # ---- Tier 3: the Pi --------------------------------------------------
        print("\n[4/5] Checking the Pi...")
        state = probe(target, session, batch=have_key)
        if state is None:
            print("  WARN: could not read the Pi's state; proceeding without checks.")
        else:
            if state.get("tar") != "1":
                print("  WARN: 'tar' not found on the Pi.")
            findings, to_install = dep_findings(
                target, state, install_ok=not args.no_deps,
                interactive=interactive, assume_yes=args.yes or args.Yes)
            if not report(findings):
                sys.exit(1)
            if to_install:
                install_deps(target, session, to_install)
            else:
                print("  OK: Python dependencies present")

        # ---- Transfer --------------------------------------------------------
        print(f"\n[5/5] Deploying to {target.host}...")
        run(ssh_argv(target, ["mkdir", "-p", REMOTE_DIR], mux=session.path))
        for filename in BACKEND_FILES:
            path = script_dir / filename
            if path.exists():
                run(scp_argv([path], f"{target}:{REMOTE_DIR}/", mux=session.path))
            else:
                print(f"  SKIP: {filename} not found")

        # config.py is never overwritten -- the Pi's copy may hold site-specific
        # URIs -- but a Pi with none at all crash-loops on `import config`, so
        # seed it when it is genuinely absent.
        if state is None:
            print("  SKIP: config.py (state unknown; not risking the Pi's copy)")
        elif state.get("config") == "1":
            print("  SKIP: config.py (the Pi's own copy is kept)")
        else:
            run(scp_argv([script_dir / "config.py"], f"{target}:{REMOTE_DIR}/",
                         mux=session.path))
            print("  OK: config.py seeded (the Pi had none)")

        run(ssh_argv(target, ["mkdir", "-p", REMOTE_WWW_PARENT], mux=session.path))
        run(scp_argv([dist_dir], f"{target}:{REMOTE_WWW_PARENT}/",
                     recursive=True, mux=session.path))
        print("  OK: frontend copied")
        if radar_present:
            run(ssh_argv(target, ["mkdir", "-p", REMOTE_RADAR_WWW_PARENT],
                         mux=session.path))
            run(scp_argv([radar_dist_dir], f"{target}:{REMOTE_RADAR_WWW_PARENT}/",
                         recursive=True, mux=session.path))
            print("  OK: radar frontend copied")

        # ---- Unit + restart --------------------------------------------------
        outcome = sync_unit(target, session, script_dir, state,
                            interactive=interactive)
        if outcome == "unchanged":
            result = subprocess.run(ssh_argv(
                target, ["sudo", "systemctl", "restart", SERVICE_NAME],
                tty=interactive, mux=session.path))
            if result.returncode != 0:
                # 255 is ssh itself (transport/auth), not the remote command.
                if result.returncode == 255:
                    print("  ERROR: ssh failed before the restart ran "
                          "(transport or authentication).")
                else:
                    print(f"  ERROR: could not restart {SERVICE_NAME} "
                          f"(exit {result.returncode}).")
                    print(f"  Check:  ssh {target} 'sudo journalctl -u "
                          f"{SERVICE_NAME} -n 50'")
                sys.exit(1)
            print("  OK: service restarted")

    finish(args.host, radar_present)
    return 0


if __name__ == "__main__":
    sys.exit(main())
