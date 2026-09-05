#!/usr/bin/env python3
"""Prepare a freshly flashed ADI Kuiper SD card so the kit comes up reachable.

Run this on your laptop, on a card you have ALREADY FLASHED with ADI Kuiper
(Raspberry Pi Imager, balenaEtcher or dd). It edits the image's FAT boot
partition; it does not write the image, and it will refuse a blank card.
Flashing is left to the tools that already do it well, with a GUI, a verify
pass, and guardrails against picking the wrong disk. It writes a handful of files to the card's FAT
boot partition and adds one entry to cmdline.txt. On first boot the Pi sets its
hostname, brings up a known fixed IP alongside DHCP, and -- if you ask it to --
provisions itself completely without you ever having to find it on the network.

    python tools/prep_sdcard.py --hostname phaser-01 --ip 192.168.7.11

This is deliberately not the deploy tool this project got rid of. It runs no
logic against the Pi, opens no ssh connection, and needs nothing installed:
it copies files onto a FAT filesystem that every operating system can mount.
Everything that actually *does* anything runs on the Pi. That distinction is
the whole reason install.sh lives where it does, and it is preserved here.

Standard library only, Python 3.8+, and no root: the boot partition automounts
writable for the invoking user on Windows, macOS and Linux alike.
"""

import argparse
import ipaddress
import os
import platform
import re
import shutil
import string
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PI_DIR = REPO_ROOT / "scripts" / "pi"

# A boot partition is identified by carrying both of these. Checking two files
# rather than one is what stops us writing to some unrelated FAT volume -- a
# camera card, a USB stick -- that happens to be mounted.
BOOT_MARKERS = ("config.txt", "cmdline.txt")

# Appended to cmdline.txt. systemd.run is the same mechanism Raspberry Pi
# Imager's own "Advanced options" use.
#
# The path is where the FAT partition will be mounted *on the running Pi*,
# which is not the same as where it is mounted on your laptop right now. It
# has been /boot for the Pi's whole history, but bookworm moved it to
# /boot/firmware, and a wrong path here means the hook silently never runs.
# Nothing on the FAT partition itself reliably distinguishes the two, so this
# defaults to /boot -- correct for Kuiper, which is bullseye-based -- and
# --boot-mount overrides it.
def cmdline_addition(boot_mount):
    return (
        f"systemd.run={boot_mount}/firstrun.sh "
        "systemd.run_success_action=reboot "
        "systemd.unit=kernel-command-line.target"
    )


BOOT_MOUNTS = ("/boot", "/boot/firmware")

DEFAULT_IP = "192.168.7.2/24"


class Failure(Exception):
    """Anything that should stop the run with a readable message."""


# --------------------------------------------------------------------------
# Finding the card
# --------------------------------------------------------------------------

def candidate_mounts():
    """Every plausible mount point for a Raspberry Pi boot partition."""
    system = platform.system()
    if system == "Windows":
        # Drive letters. The boot partition is the only part of a Pi card
        # Windows can read at all, so it always gets its own letter.
        return [Path(f"{letter}:\\") for letter in string.ascii_uppercase]
    if system == "Darwin":
        return sorted(Path("/Volumes").glob("*")) if Path("/Volumes").exists() else []
    # Linux, and WSL, where Windows drives appear under /mnt.
    roots = []
    for base in ("/media", "/run/media", "/mnt"):
        b = Path(base)
        if not b.exists():
            continue
        roots.append(b)
        # /media/<user>/<label>
        for child in sorted(b.glob("*")):
            if child.is_dir():
                roots.append(child)
    out = []
    for r in roots:
        try:
            out.extend(sorted(p for p in r.iterdir() if p.is_dir()))
        except (PermissionError, OSError):
            continue
    return out


def looks_like_boot_partition(path):
    try:
        return all((path / marker).is_file() for marker in BOOT_MARKERS)
    except (PermissionError, OSError):
        return False


def find_boot_partition():
    found = [p for p in candidate_mounts() if looks_like_boot_partition(p)]
    # Deduplicate while keeping order: WSL can surface the same volume twice.
    seen, unique = set(), []
    for p in found:
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

HOSTNAME_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$")


def validate_hostname(name):
    """RFC 1123. A bad hostname leaves the kit unreachable by name."""
    if not HOSTNAME_RE.match(name):
        raise Failure(
            f"'{name}' is not a valid hostname.\n"
            "Use letters, digits and hyphens, 1-63 characters, "
            "not starting or ending with a hyphen."
        )
    return name


def validate_cidr(value):
    """Return a normalized 'addr/prefix', accepting a bare address as /24."""
    if "/" not in value:
        value = value + "/24"
    try:
        iface = ipaddress.ip_interface(value)
    except ValueError as exc:
        raise Failure(f"'{value}' is not a valid IPv4 address: {exc}")
    if iface.version != 4:
        raise Failure("only IPv4 is supported here")
    addr = iface.ip
    if addr.is_loopback or addr.is_multicast or addr.is_unspecified:
        raise Failure(f"{addr} is not a usable host address")
    # A /32 alias cannot be reached from a laptop on the same wire, which is
    # the entire point of setting one.
    if iface.network.prefixlen > 30:
        raise Failure(
            f"prefix /{iface.network.prefixlen} is too narrow to reach; "
            "use /24 unless you know otherwise"
        )
    return str(iface)


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

def build_file_plan(hostname, cidr, autoprovision):
    """Return {filename: text} for everything that belongs on the boot partition.

    Pure: no I/O, no filesystem. tools/build_kit_image.py writes the same plan
    into an .img with mtools, while prep_sdcard.py writes it to a mounted card.
    Keeping the decision in one place is what stops a card and an image built
    from the same repo disagreeing about what a prepared kit looks like.
    """
    plan = {"phaser-hostname": hostname + "\n"}

    if cidr:
        plan["phaser-ip"] = (
            "# Fixed IP for this kit, added alongside whatever DHCP assigns.\n"
            "# One address per line, CIDR form. Edit freely -- it is read at\n"
            "# every boot. Delete the line (or the file) for DHCP only.\n"
            f"{cidr}\n"
        )
        for name in ("phaser-netalias", "phaser-netalias.service"):
            plan[name] = (PI_DIR / name).read_text(encoding="utf-8")

    if autoprovision:
        plan["phaser-autoprovision"] = (
            "# Presence of this file makes the kit provision itself on first\n"
            "# boot. The line below is the git ref to install from.\n"
            f"{autoprovision}\n"
        )

    plan["firstrun.sh"] = (PI_DIR / "firstrun.sh").read_text(encoding="utf-8")
    return plan


def patch_cmdline_text(original, boot_mount):
    """Add the first-boot hook to a cmdline.txt's text.

    Returns (new_text, changed). Raises Failure rather than returning anything
    that is not exactly one line: a cmdline.txt with a stray newline makes the
    Pi refuse to boot, with no output and nothing to debug against.
    """
    flat = " ".join(original.split())
    if "systemd.run=" in flat:
        return original, False
    new = f"{flat} {cmdline_addition(boot_mount)}\n"
    if new.strip().count("\n"):
        raise Failure("refusing to produce a multi-line cmdline.txt")
    return new, True


#: Files whose whole point is a single value the user chose. Echoing that value
#: back is a real check against a typo; echoing a line out of a shell script is
#: noise.
SINGLE_VALUE_FILES = ("phaser-hostname", "phaser-ip", "phaser-autoprovision")


def plan_detail(name, content):
    """The value to show beside a filename, or '' for a copied script."""
    if name not in SINGLE_VALUE_FILES:
        return ""
    for line in content.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return ""


def write_text(path, content, dry_run):
    """Write a file with LF endings.

    Explicitly newline='\\n': on Windows, Python would otherwise translate to
    CRLF, and a shell script or cmdline.txt with carriage returns is a genuinely
    baffling failure on the Pi.
    """
    if dry_run:
        print(f"    would write {path.name}")
        return
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)


def copy_pi_file(name, dest_dir, dry_run):
    src = PI_DIR / name
    if not src.is_file():
        raise Failure(f"missing repo file: {src}")
    if dry_run:
        print(f"    would copy {name}")
        return
    # Read and rewrite rather than shutil.copy, for the same newline reason.
    write_text(dest_dir / name, src.read_text(encoding="utf-8"), dry_run=False)


def patch_cmdline(boot, boot_mount, dry_run):
    """Add our systemd.run entry, keeping cmdline.txt a single line.

    cmdline.txt must be exactly one line. A stray newline anywhere in it makes
    the Pi refuse to boot, with no output and nothing to debug against, so this
    is careful about it and takes a backup first.
    """
    cmdline = boot / "cmdline.txt"
    original = cmdline.read_text(encoding="utf-8")
    new_text, changed = patch_cmdline_text(original, boot_mount)

    if not changed:
        print("    cmdline.txt already has a systemd.run entry; leaving it alone")
        return False

    backup = boot / "cmdline.txt.phaser-orig"
    if not backup.exists():
        write_text(backup, original, dry_run)
        if not dry_run:
            print("    backed up cmdline.txt to cmdline.txt.phaser-orig")
    else:
        print("    cmdline.txt.phaser-orig already exists; not overwriting")

    write_text(cmdline, new_text, dry_run)
    if not dry_run:
        print("    added the first-boot hook to cmdline.txt")
    return True


# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Prepare a flashed Kuiper SD card so the kit comes up reachable.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # one kit, defaults (hostname 'phaser', 192.168.7.2)
  python tools/prep_sdcard.py

  # a numbered kit with its own address
  python tools/prep_sdcard.py --hostname phaser-01 --ip 192.168.7.11

  # and let it provision itself with no ssh at all
  python tools/prep_sdcard.py --hostname phaser-01 --ip 192.168.7.11 --autoprovision

  # say where the card is, if it is not found automatically
  python tools/prep_sdcard.py --boot /mnt/e
  python tools/prep_sdcard.py --boot E:\\
""")
    ap.add_argument("--boot", metavar="PATH",
                    help="the card's boot partition; auto-detected if omitted")
    ap.add_argument("--hostname", default="phaser",
                    help="hostname for this kit (default: phaser)")
    ap.add_argument("--ip", default=DEFAULT_IP,
                    help=f"fixed IP alias, alongside DHCP (default: {DEFAULT_IP}). "
                         "'none' to skip")
    ap.add_argument("--autoprovision", nargs="?", const="main", metavar="REF",
                    help="provision unattended on first boot, from this branch "
                         "or tag (default: main). Needs internet on the Pi")
    ap.add_argument("--boot-mount", default="/boot", choices=BOOT_MOUNTS,
                    help="where the FAT partition mounts ON THE PI (not on this "
                         "laptop). /boot for Kuiper and any bullseye image; "
                         "/boot/firmware for bookworm and later")
    ap.add_argument("--dry-run", action="store_true",
                    help="say what would be written, change nothing")
    args = ap.parse_args(argv)

    # Validate everything before touching the card, so a typo cannot leave it
    # half-prepared.
    hostname = validate_hostname(args.hostname)
    cidr = None if args.ip.lower() == "none" else validate_cidr(args.ip)

    if args.boot:
        boot = Path(args.boot)
        if not boot.is_dir():
            raise Failure(f"--boot {boot} is not a directory")
        if not looks_like_boot_partition(boot):
            raise Failure(
                f"{boot} does not look like a Raspberry Pi boot partition "
                f"(expected {' and '.join(BOOT_MARKERS)} in it).\n\n"
                "This tool prepares an ALREADY-FLASHED card -- it edits the\n"
                "Kuiper image's boot partition, it does not create one. Write\n"
                "ADI Kuiper to the card first (Raspberry Pi Imager, balenaEtcher\n"
                "or dd), then re-run this.\n\n"
                "If the card is flashed, point --boot at the small FAT partition\n"
                "rather than the card's root."
            )
    else:
        found = find_boot_partition()
        if not found:
            raise Failure(
                "no Raspberry Pi boot partition found.\n\n"
                "This tool prepares an ALREADY-FLASHED card. If the card is\n"
                "blank, write ADI Kuiper to it first (Raspberry Pi Imager,\n"
                "balenaEtcher or dd) -- this tool edits the image's boot\n"
                "partition, it does not create one.\n\n"
                "If it is flashed, make sure it is mounted and re-run, or say "
                "where it is with --boot.\n"
                "  Windows:  --boot E:\\\n"
                "  WSL:      --boot /mnt/e\n"
                "  macOS:    --boot /Volumes/boot\n"
                "  Linux:    --boot /media/$USER/boot"
            )
        if len(found) > 1:
            listing = "\n".join(f"  {p}" for p in found)
            raise Failure(
                "found more than one boot partition; say which with --boot:\n"
                + listing
            )
        boot = found[0]

    print(f"Boot partition: {boot}")
    if args.dry_run:
        print("(dry run -- nothing will be written)\n")

    # Free space, before writing anything. A full FAT partition fails partway
    # through and leaves a card that boots into a half-configured state.
    if not args.dry_run:
        try:
            free = shutil.disk_usage(boot).free
            if free < 256 * 1024:
                raise Failure(f"only {free} bytes free on {boot}; need ~256 KB")
        except OSError:
            pass

    plan = build_file_plan(hostname, cidr, args.autoprovision)

    print("  Writing:")
    for name, content in plan.items():
        write_text(boot / name, content, args.dry_run)
        print(f"    {name:<24} {plan_detail(name, content)}")
    if not cidr:
        print("    (no fixed IP -- DHCP only, --ip none)")

    patch_cmdline(boot, args.boot_mount, args.dry_run)
    print(f"    (first-boot hook points at {args.boot_mount}/firstrun.sh on the Pi)")

    if args.dry_run:
        print("\nDry run complete; nothing was written.")
        return 0

    # Flush to the card before anyone yanks it. Removable media is the one
    # place where "it said it was done" and "it is actually on the disk" most
    # often differ.
    try:
        os.sync()  # type: ignore[attr-defined]
    except AttributeError:
        pass  # Windows; eject through the shell instead

    print(f"""
Done. Eject the card, put it in the Pi, and power up.

  First boot takes about a minute longer than usual: the kit sets its
  hostname, brings up the fixed IP, then reboots once by itself.

  Then reach it at:
      ssh analog@{hostname}.local""" + (f"""
      ssh analog@{str(ipaddress.ip_interface(cidr).ip)}      <- always works, no mDNS needed""" if cidr else "") + ("""

  It will provision itself from there -- give it 20-40 minutes, most of it
  building pyadi-iio. Watch it with:
      sudo journalctl -u phaser-autoprovision -f""" if args.autoprovision else """

  Then provision it:
      curl -fsSL https://raw.githubusercontent.com/livethisdream/phaser/main/scripts/provision.sh | bash""") + f"""

  If the kit does not appear at all, put the card back in your laptop and
  read firstrun.log on the boot partition. That is what it is there for.
""")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Failure as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
