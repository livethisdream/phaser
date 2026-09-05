#!/bin/sh
#
# First-boot bootstrap for a stock ADI Kuiper card. Written to the FAT boot
# partition by tools/prep_sdcard.py, which also adds
#
#     systemd.run=/boot/firstrun.sh systemd.run_success_action=reboot
#
# to cmdline.txt. That is the same mechanism Raspberry Pi Imager's own
# "Advanced options" use, so it is a well-travelled path rather than a trick.
#
# Its ONLY job is to make the kit reachable. Everything else -- overlay,
# pyadi-iio, the UI -- is provision.sh's job, over ssh, where you can watch it
# and it can fail loudly. Keep this script small: if it breaks, the kit does
# not come up on the network and there is nothing to ssh into to find out why.
#
# Which is also why it logs to the boot partition. If a kit does not appear,
# pull the card, put it in your laptop, and read firstrun.log -- the FAT
# partition is readable from Windows, macOS and Linux alike.
#
# POSIX sh, not bash: this runs before much of userspace exists.

set -u

# The boot partition is where we were launched from; find it the same way
# everything else does.
BOOTDIR=/boot
[ -f /boot/firmware/config.txt ] && BOOTDIR=/boot/firmware

exec >"$BOOTDIR/firstrun.log" 2>&1
set -x

echo "phaser firstrun: starting at $(date 2>/dev/null || echo 'unknown date')"

# systemd.run runs us early; the boot partition may be mounted read-only.
mount -o remount,rw "$BOOTDIR" 2>/dev/null || true
mount -o remount,rw / 2>/dev/null || true

read_conf() {   # filename -> first non-comment, non-blank line, trimmed
    [ -f "$BOOTDIR/$1" ] || return 1
    grep -vE '^[[:space:]]*(#|$)' "$BOOTDIR/$1" 2>/dev/null \
        | head -n1 | tr -d ' \t\r\n'
}

# ---- hostname --------------------------------------------------------------
NEW_HOSTNAME="$(read_conf phaser-hostname || true)"
if [ -n "${NEW_HOSTNAME:-}" ]; then
    if printf '%s' "$NEW_HOSTNAME" | grep -Eq '^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$'; then
        printf '%s\n' "$NEW_HOSTNAME" > /etc/hostname
        if grep -q '^127\.0\.1\.1' /etc/hosts 2>/dev/null; then
            sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$NEW_HOSTNAME/" /etc/hosts
        else
            printf '127.0.1.1\t%s\n' "$NEW_HOSTNAME" >> /etc/hosts
        fi
        echo "hostname set to $NEW_HOSTNAME"
    else
        echo "REFUSED invalid hostname '$NEW_HOSTNAME'"
    fi
fi

# ---- fixed IP alias --------------------------------------------------------
# The script and unit are carried on the boot partition by prep_sdcard.py, so
# this works with no network and no repo checkout.
if [ -f "$BOOTDIR/phaser-netalias" ]; then
    install -m 755 "$BOOTDIR/phaser-netalias" /usr/local/sbin/phaser-netalias
    install -m 644 "$BOOTDIR/phaser-netalias.service" \
        /etc/systemd/system/phaser-netalias.service
    systemctl enable phaser-netalias.service
    echo "installed phaser-netalias"
fi

# ---- ssh -------------------------------------------------------------------
# Kuiper already ships sshd enabled, so this is belt-and-braces for an image
# that does not. Both mechanisms are harmless when already satisfied.
touch "$BOOTDIR/ssh" 2>/dev/null || true
systemctl enable ssh 2>/dev/null || systemctl enable sshd 2>/dev/null || true

# ---- optional unattended provisioning --------------------------------------
# Deferred to a normal boot rather than run here: this early there is no
# network, and a 30-minute provision inside a first-boot hook is invisible if
# it stalls. The unit below runs after network-online, logs to the journal like
# anything else, and disables itself when it is done.
if [ -f "$BOOTDIR/phaser-autoprovision" ]; then
    REF="$(read_conf phaser-autoprovision || true)"
    [ -n "${REF:-}" ] || REF=main
    cat > /etc/systemd/system/phaser-autoprovision.service <<UNIT
[Unit]
Description=Phaser: unattended provisioning on first network-up
After=network-online.target
Wants=network-online.target
ConditionPathExists=!/var/lib/phaser/provisioned

[Service]
Type=oneshot
RemainAfterExit=yes
User=analog
# Long: this builds pyadi-iio from source on a Pi.
TimeoutStartSec=3600
ExecStart=/bin/sh -c 'curl -fsSL https://raw.githubusercontent.com/livethisdream/phaser/${REF}/scripts/provision.sh | bash -s -- --yes --no-reboot'
ExecStartPost=/bin/sh -c 'mkdir -p /var/lib/phaser && date -u +%%Y-%%m-%%dT%%H:%%M:%%SZ > /var/lib/phaser/provisioned'
ExecStartPost=/bin/systemctl disable phaser-autoprovision.service
# Hand back the passwordless sudo this needed. Runs on failure too (see
# ExecStopPost) so a provision that dies partway does not leave it behind.
ExecStartPost=/bin/rm -f /etc/sudoers.d/010-phaser-autoprovision
ExecStopPost=/bin/rm -f /etc/sudoers.d/010-phaser-autoprovision
ExecStartPost=/bin/systemctl reboot

[Install]
WantedBy=multi-user.target
UNIT
    # provision.sh needs passwordless sudo for an unattended run: there is
    # nobody at a keyboard to answer the prompt. This is a real grant of root
    # to the analog user, so the unit revokes it in ExecStartPost *and*
    # ExecStopPost -- the latter fires even when provisioning fails, which is
    # exactly the case where it would otherwise be left behind on a kit that
    # then sits on a workshop LAN with the stock analog/analog password.
    echo 'analog ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/010-phaser-autoprovision
    chmod 440 /etc/sudoers.d/010-phaser-autoprovision
    systemctl enable phaser-autoprovision.service
    echo "armed unattended provisioning from ref '$REF'"
fi

# ---- disarm ----------------------------------------------------------------
# Strip our own systemd.run from cmdline.txt or this runs on every boot
# forever. cmdline.txt must stay exactly one line -- a stray newline makes the
# Pi unbootable, so rewrite it with tr rather than an editor.
if [ -f "$BOOTDIR/cmdline.txt" ]; then
    sed -i 's| systemd\.run=[^ ]*||g; s| systemd\.run_success_action=[^ ]*||g; s| systemd\.unit=kernel-command-line\.target||g' \
        "$BOOTDIR/cmdline.txt"
    tr -d '\n' < "$BOOTDIR/cmdline.txt" > "$BOOTDIR/cmdline.tmp"
    mv "$BOOTDIR/cmdline.tmp" "$BOOTDIR/cmdline.txt"
    echo "cmdline.txt is now: $(cat "$BOOTDIR/cmdline.txt")"
fi

# Keep the script itself for reference but make sure it cannot run again.
mv "$BOOTDIR/firstrun.sh" "$BOOTDIR/firstrun.sh.done" 2>/dev/null || true

echo "phaser firstrun: done; rebooting"
sync
exit 0
