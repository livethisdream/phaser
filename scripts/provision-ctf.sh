#!/usr/bin/env bash
#
# Provision CTF mode on a Pi, reproducibly, without putting the flag in a repo.
#
#   ssh analog@<pi>
#   bash scripts/provision-ctf.sh --sequence "3 1 4 1 2"
#
# It prompts for the flag (silently), validates the sequence, writes
# /etc/default/phaser-ctf root-owned 0600, restarts the backend, and then
# verifies against the RUNNING service that the challenge actually loaded.
#
# Why this exists: the flag and sequence deliberately live in no repo, so the
# only record of how a kit was configured was a set of remembered ssh commands.
# That is not reproducible, and the failure mode is silent -- a kit whose
# sequence never loaded looks identical to a working one until a player cannot
# win. This script is the reproducible part; the secret stays outside it.
#
# Runs ON the Pi, matching the 2026-08-27 decision that installation does:
# sudo works normally there and the client needs nothing but ssh.

set -euo pipefail

SERVICE="phaser-headless"
ENV_FILE="/etc/default/phaser-ctf"
INSTALL_DIR="${PHASER_INSTALL_DIR:-/home/analog/pyadi-iio/examples/phaser}"
WS_PORT=8765

SEQUENCE=""
SOURCE=""
FLAG=""
FLAG_FILE=""
TOLERANCE=""
TRACK_SWEEPS=""
SIGNAL_FLOOR=""
NO_RESTART=0

say()  { printf '  %s\n' "$*"; }
step() { printf '\n[%s/5] %s\n' "$1" "$2"; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# Validate through phaser_ctf.validate_sequence and echo the normalised form.
# A separate function so a sequence given on the command line is rejected
# BEFORE the first sudo: a typo should not need a password to be told about,
# and on a non-interactive ssh the sudo prompt fails before you see the error.
validate_seq() {
    local raw="$1" out
    out="$(cd "$INSTALL_DIR" && PYTHONPATH="$INSTALL_DIR" python3 - "$raw" <<'PY'
import sys
from phaser_ctf import _parse_sequence, validate_sequence

parsed = _parse_sequence(sys.argv[1])
if parsed is None:
    print("FAIL|could not parse %r as a sector list" % sys.argv[1])
    raise SystemExit(0)
ok, reason = validate_sequence(parsed)
# On failure print only the reason: it goes straight into an error message.
if ok:
    print("OK|%s" % " ".join(str(v) for v in parsed))
else:
    print("FAIL|%s" % reason)
PY
)"
    case "$out" in
        OK\|*)   printf '%s\n' "${out#OK|}" ;;
        FAIL\|*) die "${out#FAIL|}" ;;
        *)       die "sequence validation produced no answer: ${out:-<empty>}" ;;
    esac
}

usage() {
    sed -n '3,20p' "$0" | sed 's/^# \{0,1\}//'
    cat <<'USAGE'

Options:
  --sequence "3 1 4 1 2"   Target sectors, space or comma separated.
  --source tracked|commanded
                           tracked (default) scores the measured peak: the
                           player carries the HB100. commanded scores a
                           steered ramp, the fallback for unusable RF.
  --flag-file PATH         Read the flag from a file instead of prompting.
  --tolerance DEG          Half-width of a sector (default 5).
  --track-sweeps N         Consecutive in-sector sweeps to confirm (default 3).
  --signal-floor DB        Below this the peak is noise (default -30).
  --no-restart             Write the file but leave the service alone.
  -h, --help               This text.

Anything not given is preserved from the existing file, so re-running with
just --sequence keeps the flag already in place.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --sequence)     SEQUENCE="${2:-}"; shift 2 ;;
        --source)       SOURCE="${2:-}"; shift 2 ;;
        --flag-file)    FLAG_FILE="${2:-}"; shift 2 ;;
        --tolerance)    TOLERANCE="${2:-}"; shift 2 ;;
        --track-sweeps) TRACK_SWEEPS="${2:-}"; shift 2 ;;
        --signal-floor) SIGNAL_FLOOR="${2:-}"; shift 2 ;;
        --no-restart)   NO_RESTART=1; shift ;;
        -h|--help)      usage; exit 0 ;;
        *)              die "unknown option '$1' (try --help)" ;;
    esac
done

printf '=%.0s' {1..64}; printf '\n  Phaser CTF provisioning\n'; printf '=%.0s' {1..64}; printf '\n'

# ---- 1. sanity -------------------------------------------------------------
step 1 "Checking this machine..."
[ -d "$INSTALL_DIR" ] || die "$INSTALL_DIR not found. Run this on the Pi, after install.sh."
[ -f "$INSTALL_DIR/phaser_ctf.py" ] || die "$INSTALL_DIR/phaser_ctf.py missing -- install.sh has not run here."
command -v python3 >/dev/null || die "python3 not found."
say "OK: $INSTALL_DIR on $(hostname)"

# The sidecars still work and the environment beats them, so a kit carrying
# both is configured in two places with only one of them winning. Say so
# rather than deleting someone's file.
for side in ctf_flag.txt ctf_sequence.txt; do
    [ -e "$INSTALL_DIR/$side" ] && say "NOTE: $side exists and will be shadowed by $ENV_FILE"
done

# ---- 2. validate a sequence given on the command line ----------------------
step 2 "Validating the sequence..."
CLI_SEQUENCE="$SEQUENCE"
# Same reasoning as the sequence: reject a bad --source before asking for a
# password, not after.
case "${SOURCE:-tracked}" in
    tracked|commanded) ;;
    *) die "--source must be 'tracked' or 'commanded', not '$SOURCE'." ;;
esac
if [ -n "$SEQUENCE" ]; then
    SEQUENCE="$(validate_seq "$SEQUENCE")"
    say "OK: sequence is [$SEQUENCE]"
else
    say "none given; will use whatever is already stored"
fi

# ---- 3. carry over whatever is already set ---------------------------------
step 3 "Reading existing configuration..."

# Establish sudo before the read-back, so it can tell "no such file" from
# "sudo refused". Treating the second as the first would silently drop an
# existing flag. Deliberately after the sequence check above: a typo should not
# need a password to be told about.
if ! sudo -v; then
    die "this needs sudo (it writes $ENV_FILE and restarts $SERVICE).
     Run it from an interactive shell on the Pi, not 'ssh <host> <command>'."
fi

declare -A CUR=()
if sudo test -e "$ENV_FILE"; then
    while IFS='=' read -r key value; do
        case "$key" in PHASER_CTF_*) CUR["$key"]="$value" ;; esac
    done < <(sudo cat "$ENV_FILE")
    say "found $ENV_FILE with ${#CUR[@]} setting(s)"
else
    say "no $ENV_FILE yet"
fi

[ -n "$SEQUENCE" ]     || SEQUENCE="${CUR[PHASER_CTF_SEQUENCE]:-}"
[ -n "$SOURCE" ]       || SOURCE="${CUR[PHASER_CTF_SOURCE]:-tracked}"
[ -n "$TOLERANCE" ]    || TOLERANCE="${CUR[PHASER_CTF_TOLERANCE_DEG]:-}"
[ -n "$TRACK_SWEEPS" ] || TRACK_SWEEPS="${CUR[PHASER_CTF_TRACK_SWEEPS]:-}"
[ -n "$SIGNAL_FLOOR" ] || SIGNAL_FLOOR="${CUR[PHASER_CTF_SIGNAL_FLOOR_DB]:-}"

[ -n "$SEQUENCE" ] || die "no sequence given and none stored. Use --sequence \"3 1 4 1 2\"."
case "$SOURCE" in
    tracked|commanded) ;;
    *) die "--source must be 'tracked' or 'commanded', not '$SOURCE'." ;;
esac

# A sequence carried over from the file has not been through validate_seq --
# it may predate this script, or have been hand-edited.
if [ -z "$CLI_SEQUENCE" ]; then
    SEQUENCE="$(validate_seq "$SEQUENCE")"
    say "stored sequence validates: [$SEQUENCE]"
fi

# ---- 4. the flag -----------------------------------------------------------
step 4 "Flag..."
if [ -n "$FLAG_FILE" ]; then
    [ -r "$FLAG_FILE" ] || die "cannot read --flag-file $FLAG_FILE"
    FLAG="$(head -n1 "$FLAG_FILE")"
    say "read from $FLAG_FILE"
elif [ -n "${PHASER_CTF_FLAG:-}" ]; then
    FLAG="$PHASER_CTF_FLAG"
    say "taken from the PHASER_CTF_FLAG environment variable"
elif [ -n "${CUR[PHASER_CTF_FLAG]:-}" ]; then
    FLAG="${CUR[PHASER_CTF_FLAG]}"
    say "keeping the flag already in $ENV_FILE"
else
    # Not an argument: argv is world-readable through /proc while this runs.
    printf '  Flag (not echoed): '
    IFS= read -rs FLAG
    printf '\n'
fi
[ -n "$FLAG" ] || die "no flag given."
case "$FLAG" in
    *$'\n'*) die "the flag contains a newline; EnvironmentFile cannot express that." ;;
esac

# ---- 5. write, restart, verify ---------------------------------------------
step 5 "Writing $ENV_FILE..."
TMP="$(umask 077; mktemp)"
trap 'rm -f "$TMP"' EXIT
{
    printf '# Written by scripts/provision-ctf.sh on %s\n' "$(date -Is)"
    printf '# Root-owned 0600: a service Environment= line is world-readable\n'
    printf '# via `systemctl show`, which is why this is a file.\n'
    printf 'PHASER_CTF_FLAG=%s\n' "$FLAG"
    printf 'PHASER_CTF_SEQUENCE=%s\n' "$SEQUENCE"
    printf 'PHASER_CTF_SOURCE=%s\n' "$SOURCE"
    [ -n "$TOLERANCE" ]    && printf 'PHASER_CTF_TOLERANCE_DEG=%s\n' "$TOLERANCE"
    [ -n "$TRACK_SWEEPS" ] && printf 'PHASER_CTF_TRACK_SWEEPS=%s\n' "$TRACK_SWEEPS"
    [ -n "$SIGNAL_FLOOR" ] && printf 'PHASER_CTF_SIGNAL_FLOOR_DB=%s\n' "$SIGNAL_FLOOR"
    :
} > "$TMP"
sudo install -m 600 -o root -g root "$TMP" "$ENV_FILE"
say "OK: $(sudo wc -l < "$ENV_FILE") lines, root:root 0600"

if [ "$NO_RESTART" = "1" ]; then
    say "SKIP: --no-restart given. The running service still has the old values:"
    say "      CtfMode is built at startup, so nothing here takes effect yet."
    exit 0
fi

say "Restarting $SERVICE (CtfMode reads all of this once, at startup)..."
sudo systemctl restart "$SERVICE"
sleep 4

# Verify against the running service, not by re-reading our own file: the
# question is what the backend loaded, and a restart leaves the sweep stopped,
# which is where every install lands and which stops tracked mode scoring.
cd "$INSTALL_DIR" && PYTHONPATH="$INSTALL_DIR" python3 - "$SEQUENCE" "$SOURCE" "$WS_PORT" <<'PY'
import asyncio, json, sys

expect_seq = [int(v) for v in sys.argv[1].split()]
expect_src = sys.argv[2]
port = int(sys.argv[3])


async def main():
    import websockets
    uri = "ws://localhost:%d" % port
    async with websockets.connect(uri, max_size=None) as ws:
        await ws.send(json.dumps({"cmd": "start_sweep"}))
        await asyncio.sleep(2.5)
        await ws.send(json.dumps({"cmd": "ctf_status"}))
        status = None
        end = asyncio.get_event_loop().time() + 8
        while asyncio.get_event_loop().time() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2)
            except asyncio.TimeoutError:
                break
            payload = json.loads(raw).get("data")
            if isinstance(payload, dict):
                inner = payload.get("data") if isinstance(payload.get("data"), dict) else payload
                if "progress" in inner:
                    status = inner
        if status is None:
            print("  WARN: the backend did not answer ctf_status")
            raise SystemExit(1)

        problems = []
        if not status.get("configured"):
            problems.append("configured is false -- the flag is still the placeholder")
        if status.get("source") != expect_src:
            problems.append("source is %r, expected %r" % (status.get("source"), expect_src))
        if status.get("sequence_length") != len(expect_seq):
            problems.append("sequence_length is %s, expected %d"
                            % (status.get("sequence_length"), len(expect_seq)))
        if not status.get("measuring"):
            problems.append("measuring is false -- the sweep is not running, so "
                            "tracked mode cannot score")

        # Never print the flag, even on success.
        print("  configured=%s source=%s sequence_length=%s measuring=%s "
              "sector=%s progress=%s"
              % (status.get("configured"), status.get("source"),
                 status.get("sequence_length"), status.get("measuring"),
                 status.get("current_sector"), status.get("progress")))
        for p in problems:
            print("  PROBLEM: %s" % p)
        raise SystemExit(1 if problems else 0)

asyncio.run(main())
PY
rc=$?

printf '\n'
printf '=%.0s' {1..64}; printf '\n'
if [ "$rc" = "0" ]; then
    printf '  CTF provisioned and verified against the running backend.\n'
else
    printf '  CTF written, but verification found problems (above).\n'
fi
printf '=%.0s' {1..64}; printf '\n'
say "Sequence: [$SEQUENCE]   Source: $SOURCE"
say "The flag was never printed. It is in $ENV_FILE, root-only."
exit "$rc"
