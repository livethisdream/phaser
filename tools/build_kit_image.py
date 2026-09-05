#!/usr/bin/env python3
"""Bake a ready-to-flash Phaser kit image from a stock ADI Kuiper image.

You supply a Kuiper .img or .img.xz you have already downloaded; this produces
a copy with the first-boot bootstrap already on it. Flash that to as many cards
as you like with Raspberry Pi Imager or balenaEtcher, and every kit comes up
reachable at a known address without anyone opening a terminal.

    python tools/build_kit_image.py --image ~/Downloads/kuiper.img.xz

The point of doing it here rather than per-card is that WSL cannot see a USB
card reader at all -- so a script running in WSL can never write to a card, but
it can perfectly well build an image for one. It also means preparing ten kits
is one build and ten ordinary flashes, rather than ten rounds of card surgery.

No root, and no mounting. The boot partition is written with mtools, which
edits a FAT filesystem inside a file directly. That matters more than it
sounds: a loop mount needs sudo *and* a kernel with vfat, which rules out
plenty of environments including some WSL and container setups. mtools needs
neither.

Everything about *what* goes on the card lives in tools/prep_sdcard.py and is
imported here, so a built image and a hand-prepped card cannot disagree.
"""

import argparse
import lzma
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prep_sdcard as prep  # noqa: E402

SECTOR = 512

# MBR partition types that mean "FAT". A Raspberry Pi image's first partition
# is the FAT boot partition; the second is the ext4 root, which nothing here
# touches.
FAT_TYPES = {0x01, 0x04, 0x06, 0x0B, 0x0C, 0x0E}


class Failure(prep.Failure):
    pass


# --------------------------------------------------------------------------

def read_first_sector(path):
    """The first 512 bytes of a .img or .img.xz, without expanding the whole file."""
    opener = lzma.open if path.suffix == ".xz" else open
    try:
        with opener(path, "rb") as fh:
            return fh.read(SECTOR)
    except lzma.LZMAError as exc:
        raise Failure(f"{path} is not valid xz data: {exc}")


def parse_mbr(mbr, where):
    """Return (offset_bytes, size_bytes) of the FAT partition described by an MBR."""
    if len(mbr) < SECTOR:
        raise Failure(f"{where} is too small to be a disk image")
    if mbr[510:512] != b"\x55\xaa":
        raise Failure(
            f"{where} has no MBR boot signature -- is it really a disk image?\n"
            "Pass the Kuiper .img or .img.xz exactly as downloaded."
        )

    for i in range(4):
        entry = mbr[446 + i * 16: 446 + (i + 1) * 16]
        ptype = entry[4]
        lba_start, num_sectors = struct.unpack("<II", entry[8:16])
        if ptype in FAT_TYPES and num_sectors:
            return lba_start * SECTOR, num_sectors * SECTOR

    raise Failure(
        f"no FAT boot partition found in {where}. Raspberry Pi images start "
        "with one; this may not be a Pi image."
    )


def find_fat_partition(img_path):
    """Return (offset_bytes, size_bytes) of the image's FAT boot partition.

    Parses the MBR by hand rather than shelling out to fdisk/parted: it is
    about fifteen lines, it works identically on every platform, and it needs
    nothing installed.
    """
    with open(img_path, "rb") as fh:
        return parse_mbr(fh.read(SECTOR), str(img_path))


def require_mtools():
    missing = [t for t in ("mcopy", "mtype", "mdir") if shutil.which(t) is None]
    if missing:
        raise Failure(
            f"mtools is required but {', '.join(missing)} "
            f"{'is' if len(missing) == 1 else 'are'} not on PATH.\n"
            "  Debian/Ubuntu/WSL:  sudo apt install mtools\n"
            "  macOS:              brew install mtools\n"
            "  Fedora:             sudo dnf install mtools"
        )


def _m(cmd, img, offset, *args, capture=False):
    """Run an mtools command against a partition inside an image file.

    image@@offset is mtools' own syntax for "the filesystem at this byte
    offset". MTOOLS_SKIP_CHECK quiets its complaints about Pi images, whose
    boot sectors are not quite what mtools considers canonical -- they boot on
    millions of Pis, so mtools' opinion is the thing that is wrong here.
    """
    env = dict(os.environ, MTOOLS_SKIP_CHECK="1")
    argv = [cmd, "-i", f"{img}@@{offset}", *args]
    r = subprocess.run(argv, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise Failure(f"{cmd} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout if capture else None


def read_from_image(img, offset, name):
    return _m("mtype", img, offset, f"::/{name}", capture=True)


def write_to_image(img, offset, name, content, workdir):
    """Write text into the image's FAT partition with LF endings."""
    staged = workdir / name
    with open(staged, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    _m("mcopy", img, offset, "-o", str(staged), f"::/{name}")


def decompress_or_copy(source, dest):
    """Produce a working .img at dest from a .img or .img.xz source."""
    if source.suffix == ".xz":
        print(f"  Decompressing {source.name} (this takes a minute)...")
        with lzma.open(source, "rb") as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out, length=8 * 1024 * 1024)
    else:
        print(f"  Copying {source.name}...")
        shutil.copyfile(source, dest)
    return dest


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} GB"


# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Bake a ready-to-flash Phaser kit image from a stock Kuiper image.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python tools/build_kit_image.py --image ~/Downloads/kuiper.img.xz
  python tools/build_kit_image.py --image kuiper.img --hostname phaser-01 \\
      --ip 192.168.7.11 --autoprovision

then flash the result to as many cards as you like. Per-card hostname and IP
are plain text files on the card's boot partition, editable in Notepad after
flashing -- you do not need a separate image per kit.
""")
    ap.add_argument("--image", required=True, metavar="PATH",
                    help="stock ADI Kuiper .img or .img.xz that you downloaded")
    ap.add_argument("--out", metavar="PATH",
                    help="output image (default: phaser-kit.img beside the source)")
    ap.add_argument("--hostname", default="phaser",
                    help="default hostname baked in (default: phaser); "
                         "editable per card after flashing")
    ap.add_argument("--ip", default=prep.DEFAULT_IP,
                    help=f"default fixed IP (default: {prep.DEFAULT_IP}); "
                         "'none' to skip. Editable per card after flashing")
    ap.add_argument("--autoprovision", nargs="?", const="main", metavar="REF",
                    help="bake in unattended provisioning on first boot")
    ap.add_argument("--boot-mount", default="/boot", choices=prep.BOOT_MOUNTS,
                    help="where the FAT partition mounts on the Pi "
                         "(/boot for Kuiper, /boot/firmware for bookworm)")
    ap.add_argument("--force", action="store_true",
                    help="overwrite the output image if it already exists")
    args = ap.parse_args(argv)

    require_mtools()

    source = Path(args.image).expanduser()
    if not source.is_file():
        raise Failure(f"--image {source} does not exist")

    hostname = prep.validate_hostname(args.hostname)
    cidr = None if args.ip.lower() == "none" else prep.validate_cidr(args.ip)

    if args.out:
        out = Path(args.out).expanduser()
    else:
        stem = source.name
        for suffix in (".img.xz", ".xz", ".img"):
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        out = source.parent / f"{stem}-phaser-kit.img"

    if out.resolve() == source.resolve():
        raise Failure("--out must differ from --image. Refusing to edit the "
                      "stock Kuiper image in place -- you would have no "
                      "pristine copy to rebuild from.")
    if out.exists() and not args.force:
        raise Failure(f"{out} already exists; --force to overwrite")

    # Validate the source before copying gigabytes that turn out to be wrong.
    # For an .img.xz this decompresses only the first sector.
    parse_mbr(read_first_sector(source), str(source))

    # An .img.xz expands to several GB. Running out of disk halfway leaves a
    # truncated image that looks plausible and fails at flash time.
    try:
        need = source.stat().st_size * (8 if source.suffix == ".xz" else 1)
        free = shutil.disk_usage(out.parent).free
        if free < need:
            raise Failure(
                f"not enough space in {out.parent}: need roughly "
                f"{human(need)}, have {human(free)}"
            )
    except OSError:
        pass

    print(f"Source: {source}  ({human(source.stat().st_size)})")
    print(f"Output: {out}\n")

    decompress_or_copy(source, out)
    print(f"  Wrote {human(out.stat().st_size)}")

    offset, size = find_fat_partition(out)
    print(f"  Boot partition at offset {offset} ({human(size)})\n")

    plan = prep.build_file_plan(hostname, cidr, args.autoprovision)

    with tempfile.TemporaryDirectory() as td:
        workdir = Path(td)

        print("  Writing to the boot partition:")
        for name, content in plan.items():
            write_to_image(out, offset, name, content, workdir)
            print(f"    {name:<24} {prep.plan_detail(name, content)}")

        # cmdline.txt: read what the image actually shipped, patch it, put it
        # back. Never assume its contents -- the root PARTUUID in there is
        # specific to this image and getting it wrong makes an unbootable card.
        original = read_from_image(out, offset, "cmdline.txt")
        new_text, changed = prep.patch_cmdline_text(original, args.boot_mount)
        if changed:
            write_to_image(out, offset, "cmdline.txt.phaser-orig", original, workdir)
            write_to_image(out, offset, "cmdline.txt", new_text, workdir)
            print("    cmdline.txt              + first-boot hook "
                  f"({args.boot_mount}/firstrun.sh)")
        else:
            print("    cmdline.txt              already hooked; left alone")

    print(f"""
Done: {out}

  Flash it with Raspberry Pi Imager, balenaEtcher, or:
      sudo dd if={out.name} of=/dev/sdX bs=4M status=progress conv=fsync

  Every card from this image comes up as '{hostname}'""" + (f""" at {str(cidr).split('/')[0]}""" if cidr else "") + f""".
  For a batch, give each card its own identity after flashing by editing
  these plain-text files on its boot partition -- Windows and macOS mount it
  with no extra tooling:

      phaser-hostname     phaser-01
      phaser-ip           192.168.7.11/24

  Ten cards sharing one hostname or one IP collide, so change both per kit.
""" + ("""  Each kit provisions itself on first boot; give it 20-40 minutes.
""" if args.autoprovision else """  Then provision each kit:
      ssh analog@<address>
      curl -fsSL https://raw.githubusercontent.com/livethisdream/phaser/main/scripts/provision.sh | bash
"""))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except prep.Failure as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
