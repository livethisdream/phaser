#!/usr/bin/env bash
#
# Phaser kit provisioner. RUNS ON THE PI, as the analog user.
#
#   ssh analog@analog.local        # stock Kuiper hostname, before this runs
#   curl -fsSL https://raw.githubusercontent.com/livethisdream/phaser/main/scripts/provision.sh | bash
#
# To pass options through the pipe, bash needs -s -- or it reads them as its
# own; this is the form to use when each kit gets its own name:
#
#   curl -fsSL .../scripts/provision.sh | bash -s -- --hostname phaser-01
#
# Takes a stock ADI Kuiper card to a working Phaser kit: clock, device tree
# overlay, hostname, PlutoSDR plumbing, pyadi-iio, then the browser UI via
# install.sh. Reboot when it finishes.
#
# This replaces the wget-and-run of upstream's phaser_sdcard_setup.sh
# (https://github.com/thorenscientific/rpi_setup_stuff). It does the same jobs,
# but it is safe to run twice. The upstream script is not: it does
#
#     sudo mv /boot/config.txt /boot/config_original.txt
#
# and the same for /etc/hosts and /etc/hostname, so a second run overwrites the
# pristine backups with the already-modified files and the originals are gone.
# It also replaces /boot/config.txt wholesale with a stock bullseye config from
# 2022, silently reverting whatever the running Kuiper image shipped. We merge
# the eight lines that actually matter instead. See scripts/pi/README.md.
#
# Idempotent throughout: re-running is how you bring an already-provisioned kit
# up to date, and nothing here destroys a backup it made on an earlier run.
#
# Offline:  PHASER_SRC=/path/to/repo bash scripts/provision.sh
# Offline pip too:  ... PHASER_WHEELS=/path/to/wheels

set -euo pipefail

# ---- knobs -----------------------------------------------------------------
REPO="${PHASER_REPO:-livethisdream/phaser}"
REF="${PHASER_REF:-main}"
SERVICE_USER="analog"
HOSTNAME_NEW="${PHASER_HOSTNAME:-phaser}"
TIMEZONE="${PHASER_TIMEZONE:-America/Denver}"
ASSUME_YES=0
SKIP_GUI=0
PREPARE_IMAGE=0
DO_REBOOT=""

TOTAL_STEPS=8

usage() {
    cat <<'USAGE'
Usage: provision.sh [options]

  --hostname NAME     hostname for this kit (default: phaser)
                      Give each kit its own -- ten kits called "phaser" collide
                      on mDNS and you can only reach one of them.
  --timezone TZ       IANA timezone (default: America/Denver)
  --skip-gui          stop after OS setup; do not run install.sh
  --prepare-image     arm the first-boot identity reset and clean this card
                      for imaging. Use on the golden kit only, last thing
                      before you power it off. See docs/golden-image.md.
  --yes               do not prompt (for unattended runs)
  --reboot            reboot when finished, without asking
  --no-reboot         never reboot, without asking
  -h, --help          this text

Environment:
  PHASER_SRC=DIR      install from a local copy instead of downloading
  PHASER_WHEELS=DIR   pip from a local wheel directory (fully offline)
  PHASER_REF=REF      branch or tag to download (default: main)
  GH_TOKEN=...        token, if the repo is private
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --hostname)   HOSTNAME_NEW="${2:?--hostname needs a value}"; shift 2 ;;
        --timezone)   TIMEZONE="${2:?--timezone needs a value}"; shift 2 ;;
        --skip-gui)   SKIP_GUI=1; shift ;;
        --prepare-image) PREPARE_IMAGE=1; shift ;;
        --yes|-y)     ASSUME_YES=1; shift ;;
        --reboot)     DO_REBOOT=yes; shift ;;
        --no-reboot)  DO_REBOOT=no; shift ;;
        -h|--help)    usage; exit 0 ;;
        *)            printf 'unknown option: %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done

say()  { printf '  %s\n' "$*"; }
warn() { printf '  WARNING: %s\n' "$*" >&2; }
step() { printf '\n[%s/%s] %s\n' "$1" "$TOTAL_STEPS" "$2"; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

TMP=""
SUDO_KEEPALIVE=""
cleanup() {
    [ -n "$TMP" ] && rm -rf "$TMP"
    [ -n "$SUDO_KEEPALIVE" ] && kill "$SUDO_KEEPALIVE" 2>/dev/null
    return 0
}
trap cleanup EXIT

printf '=%.0s' {1..64}; printf '\n  Phaser kit provisioner\n'; printf '=%.0s' {1..64}; printf '\n'

# ---- 1. sanity -------------------------------------------------------------
step 1 "Checking this machine..."
[ "$(id -un)" = "$SERVICE_USER" ] || die "run this as '$SERVICE_USER', not '$(id -un)'."
command -v systemctl >/dev/null 2>&1 || die "no systemd here. This expects the Kuiper Pi image."
command -v curl >/dev/null 2>&1 || die "'curl' not found."

# Find the boot partition before anything needs it. It moved to /boot/firmware
# in bookworm; Kuiper is bullseye-based and still uses /boot, but pinning
# either path would break the other.
BOOTDIR=""
for d in /boot/firmware /boot; do
    [ -f "$d/config.txt" ] && { BOOTDIR="$d"; break; }
done
[ -n "$BOOTDIR" ] || die "found no config.txt in /boot/firmware or /boot. Is this a Raspberry Pi image?"
say "OK: $(hostname), $(uname -m), boot partition at $BOOTDIR"

# Ask for sudo once, up front, rather than surprising an unattended run with a
# password prompt forty minutes in.
sudo -v || die "sudo is required."

# ...and keep it alive. A pyadi-iio build from source takes longer on a Pi than
# sudo's default 15-minute timestamp, so without this an unattended run stalls
# waiting for a password nobody is there to type. Dies with this script.
( while kill -0 "$$" 2>/dev/null; do sudo -n true 2>/dev/null; sleep 60; done ) &
SUDO_KEEPALIVE=$!

# ---- 2. clock --------------------------------------------------------------
# First, because everything below needs apt and TLS, and both fail on a wrong
# clock. Kuiper ships no NTP client at all, so `timedatectl set-ntp true` on a
# fresh kit exits "NTP not supported" -- there is nothing to enable. Seed over
# plain HTTP, then install a client, then turn it on.
step 2 "Clock and NTP..."

seed_clock_now() {
    local host hdr secs
    for host in http://deb.debian.org/ http://archive.raspberrypi.org/ http://www.google.com/; do
        hdr="$(curl -sI --max-time 10 "$host" 2>/dev/null \
               | sed -n 's/^[Dd]ate: *//p' | tr -d '\r' | head -n1)"
        [ -n "$hdr" ] || continue
        secs="$(date -u -d "$hdr" +%s 2>/dev/null)" || continue
        [ "$secs" -ge 1704067200 ] && [ "$secs" -le 4102444800 ] || continue
        sudo date -u -s "@$secs" >/dev/null 2>&1 || continue
        say "seeded clock from $host"
        return 0
    done
    return 1
}

BEFORE="$(date '+%Y-%m-%d %H:%M:%S')"
if seed_clock_now; then
    say "was $BEFORE, now $(date '+%Y-%m-%d %H:%M:%S %Z')"
else
    warn "no HTTP time source reachable; continuing with the current clock"
fi

if [ -n "$TIMEZONE" ]; then
    sudo timedatectl set-timezone "$TIMEZONE" 2>/dev/null \
        && say "timezone: $TIMEZONE" \
        || warn "could not set timezone to '$TIMEZONE' (is it a valid IANA name?)"
fi

# ---- 3. packages -----------------------------------------------------------
step 3 "Base packages..."
NEED=()
# systemd-timesyncd is a separate package since bullseye and is what
# timedatectl drives. chrony only if timesyncd is unavailable -- they conflict,
# so never both.
if ! systemctl list-unit-files 2>/dev/null | grep -Eq '^(systemd-timesyncd|chrony|ntpsec|ntp)\.service'; then
    NEED+=(systemd-timesyncd)
fi
# No RTC on this board: without fake-hwclock a cold boot starts at the epoch.
dpkg -s fake-hwclock >/dev/null 2>&1 || NEED+=(fake-hwclock)
# sshpass is what pluto_update_ad9361.sh uses to reach an attached Pluto.
command -v sshpass >/dev/null 2>&1 || NEED+=(sshpass)
command -v git >/dev/null 2>&1 || NEED+=(git)

if [ ${#NEED[@]} -eq 0 ]; then
    say "OK: nothing missing"
else
    say "Installing: ${NEED[*]}"
    if sudo apt-get update -qq 2>/dev/null; then
        if ! sudo apt-get install -y -qq "${NEED[@]}" 2>/dev/null; then
            # timesyncd absent from this suite's sources is the one failure
            # worth a second try with a different client.
            warn "apt install failed; retrying without systemd-timesyncd"
            FILTERED=()
            for p in "${NEED[@]}"; do [ "$p" = systemd-timesyncd ] || FILTERED+=("$p"); done
            [ ${#FILTERED[@]} -gt 0 ] && sudo apt-get install -y -qq "${FILTERED[@]}" 2>/dev/null || true
            sudo apt-get install -y -qq chrony 2>/dev/null && say "installed chrony instead" \
                || warn "no NTP client could be installed; the clock will rely on the HTTP seed alone"
        fi
    else
        warn "apt-get update failed (offline?); skipping package installs"
    fi
fi

# Now there is something for systemd to enable, so this finally works.
if systemctl list-unit-files 2>/dev/null | grep -q '^systemd-timesyncd\.service'; then
    sudo systemctl enable --now systemd-timesyncd >/dev/null 2>&1 || true
fi
sudo timedatectl set-ntp true >/dev/null 2>&1 \
    && say "NTP enabled" \
    || warn "could not enable NTP (no client installed, or UDP/123 blocked)"

# Persist the corrected time immediately. There is no RTC, so between here and
# the reboot at the end a power cut would otherwise throw away everything
# step 2 just fixed.
command -v fake-hwclock >/dev/null 2>&1 && sudo fake-hwclock save 2>/dev/null || true

# ---- 4. boot config --------------------------------------------------------
# Merge, never replace. The block is delimited so a re-run rewrites exactly
# these lines and leaves everything a site added around them alone.
step 4 "Device tree overlay and GPIO ($BOOTDIR/config.txt)..."

CFG="$BOOTDIR/config.txt"
BEGIN='# >>> phaser provision >>>'
END='# <<< phaser provision <<<'

# The overlay has to be reachable or the CN0566 never enumerates and every lab
# fails with a confusing IIO error rather than an obvious one.
if [ ! -f "$BOOTDIR/overlays/rpi-cn0566.dtbo" ]; then
    warn "rpi-cn0566.dtbo is not in $BOOTDIR/overlays -- the Phaser board will not"
    warn "enumerate. This image may not be ADI Kuiper. Continuing anyway."
fi

# One pristine backup, taken once, never overwritten. This is the bug in the
# upstream script: it backs up on every run, so the second run's "original" is
# the first run's edited copy.
if [ ! -f "$CFG.phaser-orig" ]; then
    sudo cp "$CFG" "$CFG.phaser-orig"
    say "backed up the original to $CFG.phaser-orig"
else
    say "original backup already exists; left alone"
fi

# [all] first: config.txt is sectioned, and appending after a trailing [pi4] or
# [cm4] filter would silently scope these to one board revision.
BLOCK="$(cat <<EOF
$BEGIN
# Managed by scripts/provision.sh (livethisdream/phaser). Edits inside this
# block are replaced on re-provision; put your own settings outside it.
[all]
# CN0566 Phaser board:
dtoverlay=rpi-cn0566
# Green activity LED as a heartbeat, so a headless kit shows it is alive:
dtparam=act_led_gpio=26
dtparam=act_led_trigger=heartbeat
# Short pin 40 to ground for a clean shutdown:
dtoverlay=gpio-shutdown,gpio_pin=21,active_low=1,gpiopull=up
$END
EOF
)"

if grep -qF "$BEGIN" "$CFG"; then
    # Replace in place. awk rather than sed: the block spans lines and contains
    # characters sed would treat as delimiters.
    TMPCFG="$(mktemp)"
    awk -v begin="$BEGIN" -v end="$END" -v block="$BLOCK" '
        $0 == begin { print block; skip = 1; next }
        $0 == end   { skip = 0; next }
        !skip       { print }
    ' "$CFG" > "$TMPCFG"
    if cmp -s "$TMPCFG" "$CFG"; then
        say "OK: already current"
    else
        sudo cp "$TMPCFG" "$CFG"
        say "updated the phaser block"
    fi
    rm -f "$TMPCFG"
else
    printf '\n%s\n' "$BLOCK" | sudo tee -a "$CFG" >/dev/null
    say "added the phaser block"
fi

# ---- 5. hostname -----------------------------------------------------------
step 5 "Hostname..."
CURRENT="$(hostname)"
if [ "$CURRENT" = "$HOSTNAME_NEW" ]; then
    say "OK: already '$HOSTNAME_NEW'"
else
    printf '%s' "$HOSTNAME_NEW" | grep -Eq '^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$' \
        || die "'$HOSTNAME_NEW' is not a valid hostname (letters, digits, hyphens; no leading or trailing hyphen)."
    sudo hostnamectl set-hostname "$HOSTNAME_NEW"
    # hostnamectl does not touch /etc/hosts, and sudo warns on every command
    # until 127.0.1.1 resolves to the new name.
    if grep -q '^127\.0\.1\.1' /etc/hosts; then
        sudo sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$HOSTNAME_NEW/" /etc/hosts
    else
        printf '127.0.1.1\t%s\n' "$HOSTNAME_NEW" | sudo tee -a /etc/hosts >/dev/null
    fi
    say "$CURRENT -> $HOSTNAME_NEW (reachable as ${HOSTNAME_NEW}.local after reboot)"
fi

# ---- 6. PlutoSDR plumbing --------------------------------------------------
# A udev rule launches a second iiod bound to the Pluto when it is plugged in,
# and tears it down when it is unplugged, so the Pluto can be connected after
# boot and reconnected freely.
step 6 "PlutoSDR USB plumbing..."

# Source is needed from here on: steps 6-8 install files out of the repo.
TMP="$(mktemp -d)"
SRC="$TMP/src"
mkdir -p "$SRC"
if [ -n "${PHASER_SRC:-}" ]; then
    [ -d "$PHASER_SRC" ] || die "PHASER_SRC=$PHASER_SRC is not a directory."
    say "using local source at $PHASER_SRC"
    tar -cf - -C "$PHASER_SRC" . | tar -xf - -C "$SRC"
else
    TOKEN="${GH_TOKEN:-${PHASER_TOKEN:-${GITHUB_TOKEN:-}}}"
    AUTH=()
    [ -n "$TOKEN" ] && AUTH=(-H "Authorization: Bearer $TOKEN")
    say "downloading ${REPO}@${REF}..."
    curl -fsSL "${AUTH[@]}" -H "X-GitHub-Api-Version: 2022-11-28" \
         "https://api.github.com/repos/${REPO}/tarball/${REF}" \
      | tar -xz -C "$SRC" --strip-components=1 \
      || die "download failed. Check '${REF}' is a real branch or tag, or use
     PHASER_SRC=/path/to/repo bash scripts/provision.sh"
fi
[ -f "$SRC/phaser_headless.py" ] || die "that does not look like the phaser repo."

install_if_changed() {   # src dst mode label
    local s="$1" d="$2" m="$3" label="$4"
    if [ -e "$d" ] && cmp -s "$s" "$d"; then
        say "OK: $label already current"
        return 1
    fi
    sudo install -m "$m" "$s" "$d"
    say "installed $label"
    return 0
}

UDEV_CHANGED=0
install_if_changed "$SRC/scripts/pi/89-pluto.rules" /etc/udev/rules.d/89-pluto.rules 644 \
    "udev rule" && UDEV_CHANGED=1
install_if_changed "$SRC/scripts/pi/iiod-usb@.service" '/etc/systemd/system/iiod-usb@.service' 644 \
    "iiod template unit" && UDEV_CHANGED=1
if [ "$UDEV_CHANGED" = 1 ]; then
    sudo systemctl daemon-reload
    sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=usb || true
fi

# Convenience tool, not part of provisioning -- run by hand when a Pluto needs
# reflashing to AD9361 2r2t mode.
install -m 755 "$SRC/scripts/pi/pluto_update_ad9361.sh" "/home/$SERVICE_USER/pluto_update_ad9361.sh"
say "pluto_update_ad9361.sh is in ~ (run it by hand when a Pluto needs it)"

# Clock unit, now that we have the source.
install_if_changed "$SRC/scripts/pi/phaser-clock" /usr/local/sbin/phaser-clock 755 "clock script" || true
if install_if_changed "$SRC/scripts/pi/phaser-clock.service" \
       /etc/systemd/system/phaser-clock.service 644 "clock unit"; then
    sudo systemctl daemon-reload
fi
sudo systemctl enable phaser-clock.service >/dev/null 2>&1 || true

# ---- 7. pyadi-iio ----------------------------------------------------------
# Upstream uninstalls and rebuilds from the tip of main on every run. That is
# slow (minutes on a Pi), needs a network, and silently moves a working
# workshop kit onto whatever landed upstream this morning. Check instead, and
# only build when the check fails.
step 7 "pyadi-iio..."
if python3 -c "import adi; adi.CN0566" >/dev/null 2>&1; then
    say "OK: adi.CN0566 imports; leaving the installed pyadi-iio alone"
else
    say "adi.CN0566 does not import; installing pyadi-iio from source"
    PYADI_DIR="/home/$SERVICE_USER/pyadi-iio"
    if [ -d "$PYADI_DIR/.git" ]; then
        say "updating the existing clone at $PYADI_DIR"
        git -C "$PYADI_DIR" fetch --quiet origin main && git -C "$PYADI_DIR" checkout --quiet main \
            && git -C "$PYADI_DIR" reset --hard --quiet origin/main \
            || warn "could not update the clone; building what is there"
    else
        git clone --quiet --depth 1 https://github.com/analogdevicesinc/pyadi-iio.git "$PYADI_DIR" \
            || die "could not clone pyadi-iio (offline? then pre-stage it at $PYADI_DIR)"
    fi
    PIP_SRC=()
    if [ -n "${PHASER_WHEELS:-}" ]; then
        [ -d "$PHASER_WHEELS" ] || die "PHASER_WHEELS=$PHASER_WHEELS is not a directory."
        PIP_SRC=(--no-index --find-links "$PHASER_WHEELS")
    fi
    # Remove a stale build first: the failure mode we are fixing is an older
    # pyadi-iio that imports but has no CN0566.
    sudo python3 -m pip uninstall -y pyadi-iio >/dev/null 2>&1 || true
    sudo python3 -m pip install "${PIP_SRC[@]}" "$PYADI_DIR" \
      || sudo python3 -m pip install --break-system-packages "${PIP_SRC[@]}" "$PYADI_DIR" \
      || die "pip failed to install pyadi-iio."
    sudo ldconfig || true
    python3 -c "import adi; adi.CN0566" >/dev/null 2>&1 \
        && say "OK: adi.CN0566 now imports" \
        || die "installed pyadi-iio, but adi.CN0566 still does not import."
fi

# ---- 8. the browser UI -----------------------------------------------------
step 8 "Browser UI (install.sh)..."
if [ "$SKIP_GUI" = 1 ]; then
    say "SKIPPED (--skip-gui)"
else
    # PHASER_SRC so install.sh reuses the tree we already downloaded rather
    # than fetching the same tarball a second time.
    PHASER_SRC="$SRC" bash "$SRC/install.sh"
fi

# ---- optional: arm for imaging ---------------------------------------------
if [ "$PREPARE_IMAGE" = 1 ]; then
    printf '\n[extra] Preparing this card for imaging...\n'
    sudo install -m 755 "$SRC/scripts/pi/phaser-firstboot" /usr/local/sbin/phaser-firstboot
    sudo install -m 644 "$SRC/scripts/pi/phaser-firstboot.service" \
         /etc/systemd/system/phaser-firstboot.service
    sudo rm -f /var/lib/phaser/firstboot-done
    sudo systemctl daemon-reload
    sudo systemctl enable phaser-firstboot.service >/dev/null 2>&1
    say "armed phaser-firstboot (regenerates SSH host keys, machine-id, hostname)"

    # A hostname file on the FAT partition, so each cloned card can be named
    # from a Windows laptop with Notepad.
    if [ ! -f "$BOOTDIR/phaser-hostname" ]; then
        printf 'phaser\n' | sudo tee "$BOOTDIR/phaser-hostname" >/dev/null
        say "created $BOOTDIR/phaser-hostname (edit it per card after flashing)"
    fi

    sudo apt-get clean 2>/dev/null || true
    sudo journalctl --rotate >/dev/null 2>&1 || true
    sudo journalctl --vacuum-time=1s >/dev/null 2>&1 || true
    rm -f "/home/$SERVICE_USER/.bash_history" "/home/$SERVICE_USER/.ssh/known_hosts" 2>/dev/null || true
    say "cleaned apt cache, journal, shell history"
    say "Power off with 'sudo shutdown -h now', then image the card."
    say "See docs/golden-image.md for the rest."
fi

# ---- summary ---------------------------------------------------------------
printf '\n'; printf '=%.0s' {1..64}; printf '\n'
say "Provisioned."
say ""
say "  hostname   $HOSTNAME_NEW  (http://${HOSTNAME_NEW}.local:8080/ after reboot)"
say "  clock      $(date '+%Y-%m-%d %H:%M:%S %Z')  NTP: $(timedatectl show -p NTP --value 2>/dev/null || echo unknown)"
say "  overlay    merged into $CFG (original at $CFG.phaser-orig)"
say ""
say "A reboot is required: the device tree overlay and the hostname only take"
say "effect at boot, so the Phaser board will not enumerate until you do."
printf '=%.0s' {1..64}; printf '\n'

if [ -z "$DO_REBOOT" ]; then
    if [ "$ASSUME_YES" = 1 ]; then
        DO_REBOOT=no
    elif [ -t 0 ]; then
        read -rp $'\nReboot now? [y/N] ' ans
        case "$ans" in [Yy]*) DO_REBOOT=yes ;; *) DO_REBOOT=no ;; esac
    else
        # Piped from curl, so stdin is the script itself, not a terminal.
        DO_REBOOT=no
    fi
fi

if [ "$DO_REBOOT" = yes ]; then
    say "Rebooting..."
    sudo reboot
else
    say "Reboot when ready:  sudo reboot"
fi
