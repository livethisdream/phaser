#!/usr/bin/env bash
#
# Phaser installer. RUNS ON THE PI.
#
#   ssh analog@phaser.local
#   curl -fsSL https://raw.githubusercontent.com/livethisdream/phaser/main/install.sh | bash
#
# Two lines, from any operating system, because line one is just ssh and line
# two runs here. Set PHASER_REF to install a branch or tag other than main.
# If the repo is private, export GH_TOKEN first (read -rsp keeps it out of
# your shell history).
#
# Why this runs on the Pi rather than on your laptop: the Pi is the one machine
# whose environment we control. Every deployment bug this project has had came
# from the *client* side -- cmd.exe not expanding globs, PATHEXT, no
# ControlMaster on Windows OpenSSH, BatchMode refusing password auth, ssh -t
# defeated by a stdin redirect, ConPTY and sudo, a Microsoft Store alias
# masquerading as python. None of that is about installing Phaser. Move the
# logic here and the client needs nothing but ssh, which every OS ships.
#
# It also means sudo simply works: you are sitting in an interactive shell, so
# it prompts the way it always has. No pty gymnastics.
#
# Idempotent. Safe on a fresh Pi, and safe on a dirty one -- it updates a
# drifted systemd unit, replaces the frontend atomically, and never overwrites
# a config.py that already exists.
#
# Offline / no-token install: fetch the repo however you like, then
#   PHASER_SRC=/path/to/repo bash install.sh
#
# Fully offline, including the Python packages:
#   PHASER_SRC=/path/to/repo PHASER_WHEELS=/path/to/wheels bash install.sh

set -euo pipefail

REPO="${PHASER_REPO:-livethisdream/phaser}"
REF="${PHASER_REF:-main}"
SERVICE="phaser-headless"
SERVICE_USER="analog"
INSTALL_DIR="/home/${SERVICE_USER}/pyadi-iio/examples/phaser"
UNIT_PATH="/etc/systemd/system/${SERVICE}.service"
PYTHON_BIN="/usr/bin/python3"
HTTP_PORT=8080

# ctf_flag.txt and ctf_sequence.txt are deliberately absent: this is an
# allowlist, so the CTF's secrets are excluded by construction rather than by
# an exclusion rule someone has to remember to maintain. They live on the Pi
# only, the same treatment config.py gets below.
BACKEND_FILES=(
  phaser_headless.py phaser_cal_headless.py phaser_find_hb100_headless.py
  phaser_characterize_headless.py phaser_gain_curve_headless.py
  phaser_cw_radar.py phaser_radar_dsp.py phaser_fmcw_radar.py ADAR_pyadi_functions.py SDR_functions.py phaser_functions.py
  phaser_ctf.py
  LTE5_MHz.ftr LTE10_MHz.ftr LTE20_MHz.ftr
)

say()  { printf '  %s\n' "$*"; }
step() { printf '\n[%s/6] %s\n' "$1" "$2"; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

TMP=""
cleanup() { [ -n "$TMP" ] && rm -rf "$TMP"; }
trap cleanup EXIT

printf '=%.0s' {1..64}; printf '\n  Phaser installer\n'; printf '=%.0s' {1..64}; printf '\n'

# ---- 1. sanity -------------------------------------------------------------
step 1 "Checking this machine..."
[ -x "$PYTHON_BIN" ] || die "$PYTHON_BIN not found. This expects the Phaser Pi image."
id "$SERVICE_USER" >/dev/null 2>&1 || die "user '$SERVICE_USER' does not exist. This expects the Phaser Pi image."
[ "$(id -un)" = "$SERVICE_USER" ] || die "run this as '$SERVICE_USER', not '$(id -un)'. Everything below writes into that user's home."
command -v tar >/dev/null 2>&1 || die "'tar' not found."
say "OK: running as $SERVICE_USER on $(hostname), $($PYTHON_BIN -V 2>&1)"

# ---- 2. get the source -----------------------------------------------------
step 2 "Getting the source..."
TMP="$(mktemp -d)"
SRC="$TMP/src"
mkdir -p "$SRC"

if [ -n "${PHASER_SRC:-}" ]; then
    [ -d "$PHASER_SRC" ] || die "PHASER_SRC=$PHASER_SRC is not a directory."
    say "Using local source at $PHASER_SRC (no download)"
    tar -cf - -C "$PHASER_SRC" . | tar -xf - -C "$SRC"
else
    # No token needed for a public repo. GH_TOKEN is only for a private one --
    # the same endpoint serves both, so there is one code path either way.
    TOKEN="${GH_TOKEN:-${PHASER_TOKEN:-${GITHUB_TOKEN:-}}}"
    AUTH=()
    [ -n "$TOKEN" ] && AUTH=(-H "Authorization: Bearer $TOKEN")
    say "Downloading ${REPO}@${REF}${TOKEN:+ (authenticated)}..."
    # --strip-components=1: GitHub wraps the tree in a <owner>-<repo>-<sha> dir.
    if ! curl -fsSL "${AUTH[@]}" -H "X-GitHub-Api-Version: 2022-11-28" \
              "https://api.github.com/repos/${REPO}/tarball/${REF}" \
         | tar -xz -C "$SRC" --strip-components=1; then
        if [ -z "$TOKEN" ]; then
            die "download failed. If ${REPO} is private, provide a token:
       read -rsp 'GitHub token: ' GH_TOKEN && export GH_TOKEN && echo
     and re-run. Otherwise check that '${REF}' is a real branch or tag,
     or install from a local copy:  PHASER_SRC=/path/to/repo bash install.sh"
        fi
        die "download failed. Check the token has Contents:Read on ${REPO},
     that it has not expired, and that '${REF}' is a real branch or tag."
    fi
fi
[ -f "$SRC/phaser_headless.py" ] || die "the source does not look like the phaser repo (no phaser_headless.py)."
say "OK: source ready"

# ---- 3. python dependencies ------------------------------------------------
step 3 "Python dependencies..."
# Only what is actually missing, and never --upgrade: this script's job is
# "make sure these import", not "keep them latest". A working workshop Pi
# should not have a known-good pyzmq swapped out because someone re-ran it.
MISSING=""
for pair in zmq:pyzmq msgpack:msgpack websockets:websockets; do
    mod="${pair%%:*}"; pkg="${pair##*:}"
    "$PYTHON_BIN" -c "import $mod" >/dev/null 2>&1 || MISSING="$MISSING $pkg"
done
if [ -z "$MISSING" ]; then
    say "OK: all present"
else
    say "Installing:$MISSING"
    # PHASER_WHEELS points at a directory of pre-downloaded wheels, which is the
    # only way a Pi with no internet can be provisioned from scratch: everything
    # else here works offline, but pip does not. --no-index makes the failure
    # honest -- if a wheel is absent it says so rather than silently reaching
    # for the network and hanging on a Pi that has none.
    PIP_SRC=()
    if [ -n "${PHASER_WHEELS:-}" ]; then
        [ -d "$PHASER_WHEELS" ] || die "PHASER_WHEELS=$PHASER_WHEELS is not a directory."
        say "Using local wheels from $PHASER_WHEELS (no network)"
        PIP_SRC=(--no-index --find-links "$PHASER_WHEELS")
    fi
    # A bookworm-based image marks the system Python PEP 668 externally-managed,
    # so a plain --user install exits non-zero. Bullseye needs no such thing.
    # No sudo: --user writes to this user's ~/.local.
    "$PYTHON_BIN" -m pip install --user "${PIP_SRC[@]}" $MISSING \
      || "$PYTHON_BIN" -m pip install --user --break-system-packages "${PIP_SRC[@]}" $MISSING \
      || die "pip failed.${PHASER_WHEELS:+
     Check $PHASER_WHEELS holds cp39 linux_armv7l wheels for:$MISSING}"
    "$PYTHON_BIN" -c "import zmq, msgpack, websockets" \
      || die "installed, but not importable as $SERVICE_USER -- the service would crash-loop."
    say "OK: installed and importable"
fi

# ---- 4. place the files ----------------------------------------------------
step 4 "Installing files to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
for f in "${BACKEND_FILES[@]}"; do
    [ -f "$SRC/$f" ] && cp -f "$SRC/$f" "$INSTALL_DIR/" || say "SKIP: $f not in source"
done

# config.py is never overwritten -- the Pi's copy may hold site-specific URIs
# and calibration. But a Pi with none at all crash-loops, because
# phaser_headless.py does `import config` -> sys.exit(1) at module level.
if [ -e "$INSTALL_DIR/config.py" ]; then
    say "KEEP: config.py (site config preserved)"
else
    cp "$SRC/config.py" "$INSTALL_DIR/config.py"
    say "OK: config.py seeded (there was none)"
fi

# Frontend: replace rather than merge. Vite emits content-hashed filenames, so
# copying over the top accumulates every old build's assets forever.
if [ -f "$SRC/frontend/dist/index.html" ]; then
    # Never install a simulator onto hardware. The Pages demo is built from
    # this same source with VITE_TRANSPORT=sim, and Vite folds that flag away
    # at build time -- the two builds' index.html were byte-identical until
    # frontend/vite.config.js started stamping the marker below, so a sim build
    # committed to dist/ by mistake would install here and the lab would run on
    # synthesized IQ that looks entirely plausible. The only other tell is a
    # pill in the corner of the UI.
    #
    # An older dist/ predating the marker has no meta tag at all; that is not an
    # error, it just cannot be checked. Only an explicit "sim" is refused.
    FE_MODE="$(sed -n 's/.*<meta name="phaser-transport" content="\([a-z]*\)".*/\1/p' \
               "$SRC/frontend/dist/index.html" | head -n1)"
    if [ "$FE_MODE" = "sim" ] && [ -z "${PHASER_ALLOW_SIM_FRONTEND:-}" ]; then
        die "the frontend in this source is a SIMULATOR build, not a hardware build.
     It would show synthesized data on real hardware.
     Rebuild it with:  cd frontend && npm run build
     Or, if a simulator UI is genuinely what you want on this Pi, re-run with
     PHASER_ALLOW_SIM_FRONTEND=1. For a one-off, ?sim=1 on the URL is better --
     it needs no reinstall and leaves the real build in place."
    fi
    if [ "$FE_MODE" = "sim" ]; then
        say "WARN: installing a SIMULATOR frontend (PHASER_ALLOW_SIM_FRONTEND set)"
    fi

    rm -rf "$INSTALL_DIR/frontend/dist.new"
    mkdir -p "$INSTALL_DIR/frontend"
    cp -r "$SRC/frontend/dist" "$INSTALL_DIR/frontend/dist.new"
    rm -rf "$INSTALL_DIR/frontend/dist"
    mv "$INSTALL_DIR/frontend/dist.new" "$INSTALL_DIR/frontend/dist"
    say "OK: frontend installed"
else
    die "no frontend build in the source (frontend/dist/index.html).
     CI commits it; check out a ref that has it."
fi
if [ -f "$SRC/frontend-radar/dist/index.html" ]; then
    rm -rf "$INSTALL_DIR/frontend-radar/dist.new"
    mkdir -p "$INSTALL_DIR/frontend-radar"
    cp -r "$SRC/frontend-radar/dist" "$INSTALL_DIR/frontend-radar/dist.new"
    rm -rf "$INSTALL_DIR/frontend-radar/dist"
    mv "$INSTALL_DIR/frontend-radar/dist.new" "$INSTALL_DIR/frontend-radar/dist"
    say "OK: radar frontend installed"
fi
# A stray unit file here would be served by the static-file fallback.
rm -f "$INSTALL_DIR/${SERVICE}.service"

# ---- 5. systemd unit -------------------------------------------------------
step 5 "systemd unit..."
TPL="$SRC/scripts/${SERVICE}.service.template"
[ -f "$TPL" ] || die "unit template missing from the source ($TPL)."
RENDERED="$TMP/${SERVICE}.service"
sed -e "s|@USER@|${SERVICE_USER}|g" \
    -e "s|@INSTALL_DIR@|${INSTALL_DIR}|g" \
    -e "s|@PYTHON@|${PYTHON_BIN}|g" "$TPL" > "$RENDERED"

# Compare content, not existence. Checking only that the file exists meant a Pi
# provisioned by an older version kept its old unit forever, so template
# changes never reached machines already in the field.
NEED_UNIT=1
if [ -e "$UNIT_PATH" ] && cmp -s "$RENDERED" "$UNIT_PATH"; then
    NEED_UNIT=0
    say "OK: unit is already current"
elif [ -e "$UNIT_PATH" ]; then
    say "The installed unit differs from this version; updating it."
else
    say "No unit installed yet; installing it."
fi

if [ "$NEED_UNIT" = "1" ]; then
    say "sudo is needed to write $UNIT_PATH -- you may be asked for your password."
    sudo install -m 644 "$RENDERED" "$UNIT_PATH"
    sudo systemctl daemon-reload
    sudo systemctl enable "$SERVICE" >/dev/null
    say "OK: unit installed and enabled"
fi

# ---- 5b. let the GUI power the machine off ---------------------------------
# Standard on every install: holding the connection pill in the UI shuts the kit
# down, the same clean poweroff as the push button on the Phaser itself. The
# backend runs unprivileged and cannot do that alone, so it needs this rule.
#
# The rule is a single command. It grants no other privilege, and deleting the
# file revokes it -- though the next install.sh will write it back.
#
# Worth knowing: the backend is unauthenticated, so anyone who can reach it can
# power the machine off. On a shared or public network, that is a power switch
# for the room.
SUDOERS_PATH="/etc/sudoers.d/phaser-shutdown"
step 5b "Granting the GUI permission to power off..."
RENDERED_SUDO="$TMP/phaser-shutdown"
{
    printf '# Installed by phaser install.sh.\n'
    printf '# Exactly one command; delete this file to revoke.\n'
    printf '%s ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff\n' "$SERVICE_USER"
} > "$RENDERED_SUDO"
# visudo -c refuses a file that would otherwise break sudo entirely.
if sudo visudo -cqf "$RENDERED_SUDO"; then
    sudo install -m 440 -o root -g root "$RENDERED_SUDO" "$SUDOERS_PATH"
    say "OK: $SUDOERS_PATH installed"
else
    die "the generated sudoers rule failed validation; refusing to install it."
fi

# ---- 6. start and verify ---------------------------------------------------
step 6 "Starting the service..."
sudo systemctl restart "$SERVICE"
sleep 4
if ! systemctl is-active --quiet "$SERVICE"; then
    printf '\nERROR: %s did not stay running. Last 30 log lines:\n\n' "$SERVICE" >&2
    sudo journalctl -u "$SERVICE" -n 30 --no-pager >&2 || true
    exit 1
fi
# curl prints 000 itself when nothing answers and then exits non-zero, so an
# `|| echo 000` here appended a second one ("HTTP 000000"). Retry briefly: the
# backend starts the hardware before its web server.
CODE=000
for _ in 1 2 3 4 5 6 7 8; do
    CODE="$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${HTTP_PORT}/" 2>/dev/null || true)"
    CODE="${CODE:-000}"
    [ "$CODE" = "200" ] && break
    sleep 2
done
[ "$CODE" = "200" ] || say "WARN: the UI returned HTTP $CODE (expected 200)"

# ---- 6b. the RF chain this kit will actually run ---------------------------
#
# config.py is site-owned and never deployed over, so the numbers that decide
# the chain are exactly the ones nobody reviews. Printing them is most of the
# value: one kit turned out to be carrying Rx_gain = 1 and an IF 300 MHz off
# the repo default, neither of which is visible anywhere else.
#
# A deaf chain cannot be detected at runtime -- the write is accepted, the
# readback agrees, and MUXOUT is not wired as lock detect on this board -- so
# a check before the kit reaches a table is the only one there is.
#
# What that check compares against is now this unit's OWN measured IF window
# where it has one (phaser_characterize_headless.py), not a constant. The
# constant was the bug: the usable IF differs by ~200 MHz between kits, the
# figure in the repo came off one of them, and the other was deaf at the
# shipped config.Rx_freq with nothing anywhere saying so.
#
# Warn, never fail. An uncharacterized kit is the normal state of a fresh
# install, and refusing to install over it would turn a warning into a dead
# workshop.
step 6b "Checking the RF chain..."
PYTHONPATH="$INSTALL_DIR" "$PYTHON_BIN" - "$INSTALL_DIR" <<'PY' || say "WARN: could not evaluate the RF chain"
import os
import sys

sys.path.insert(0, sys.argv[1])
os.chdir(sys.argv[1])

try:
    import config
    from phaser_functions import (lo_chain, rf_chain_warnings, load_hb100_cal,
                                  load_if_cal, rx_freq_source)
except Exception as exc:                      # noqa: BLE001 - report, never fail
    print("  could not import the installed backend: %s" % exc)
    raise SystemExit(0)

chosen = rx_freq_source(config)
rx_freq = chosen["rx_freq_hz"]

try:
    signal = load_hb100_cal()
    source = "calibration.json"
except Exception:
    signal = float(getattr(config, "SignalFreq", 0) or 0)
    source = "config.py default (no HB100 calibration yet)"

if not signal:
    print("  no signal frequency to work from; skipping")
    raise SystemExit(0)

chain = lo_chain(signal, rx_freq)
window = load_if_cal()
print("  HB100      %.6f GHz   (%s)" % (signal / 1e9, source))
print("  IF         %.3f GHz      (%s)" % (rx_freq / 1e9, chosen["source"]))
print("  LO         %.3f GHz      = HB100 + IF" % (chain["lo_hz"] / 1e9))
print("  ADF4159    %.4f GHz     = LO / 4" % (chain["adf4159_hz"] / 1e9))
if window is not None:
    print("  IF window  %.3f - %.3f GHz   (measured on this unit)"
          % (float(window["clean_min_hz"]) / 1e9,
             float(window["clean_max_hz"]) / 1e9))

problems = list(chosen["warnings"]) + rf_chain_warnings(signal, rx_freq)
for line in problems:
    print("  WARN: %s" % line)
if not problems:
    print("  OK: inside the IF window measured on this unit")

# A fresh kit has no measurement, and that is the normal state after an
# install rather than an error. Leave a marker for the closing summary: the
# three setup scripts have to run in order and nothing else says so.
if window is None:
    open("/tmp/.phaser-uncharacterized", "w").close()
PY

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
printf '\n'; printf '=%.0s' {1..64}; printf '\n'
if [ "$CODE" = "200" ]; then
    say "Installed. Service is active and the UI answered HTTP 200."
else
    # Not a failure on a freshly provisioned kit: the board's device-tree
    # overlay only loads at boot, so the backend cannot start the hardware
    # until the reboot provision.sh asks for. Say so instead of "success".
    say "Installed, but the UI is not answering yet (HTTP $CODE)."
    say "  On a kit just provisioned, reboot first; the board appears at boot."
    say "  Otherwise:  sudo journalctl -u ${SERVICE} -n 30   ('No device found' = the Pluto's USB cable)"
fi

# Deliberately NOT run from here, nor from provision.sh. Characterization
# needs a powered HB100 aimed at the array, and provisioning runs unattended
# at first boot (scripts/pi/firstrun.sh passes --yes), often before the kit is
# even assembled -- sweeping then would measure noise. Telling the operator is
# the most an installer can honestly do.
if [ -f /tmp/.phaser-uncharacterized ]; then
    rm -f /tmp/.phaser-uncharacterized
    say ""
    say "This kit has NOT been characterized. The usable IF differs between"
    say "kits by ~200 MHz, and the config default is one that some kits cannot"
    say "hear at all -- a deaf kit looks exactly like a dead HB100."
    say ""
    say "With the HB100 powered and aimed at the array, run once:"
    say ""
    say "  cd $INSTALL_DIR"
    say "  python3 phaser_find_hb100_headless.py       # find the source"
    say "  python3 phaser_characterize_headless.py     # find the IF"
    say "  python3 phaser_cal_headless.py              # calibrate the array"
    say ""
    say "Or press Find HB100 / Characterize IF / Calibrate in the UI, in that"
    say "order. See docs/calibration-bugs.md."
fi
say ""
say "  http://$(hostname).local:${HTTP_PORT}/"
[ -n "$IP" ] && say "  http://${IP}:${HTTP_PORT}/"
say ""
say "Logs:     sudo journalctl -u ${SERVICE} -f"
say "Re-run:   this script is idempotent; run it again to update."
printf '=%.0s' {1..64}; printf '\n'
