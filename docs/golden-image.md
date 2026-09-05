# Cloning a batch of Phaser kits

Provisioning one kit takes tens of minutes, most of it building pyadi-iio from
source on a Pi. Doing that ten times over is a waste of an afternoon.

**There are two ways to avoid it, and they suit different situations.**

| | `build_kit_image.py` | Golden image (this document) |
| --- | --- | --- |
| Start from | Stock Kuiper image | A kit you provisioned and verified |
| Per-kit first boot | Provisions itself (~30 min, needs internet) | Already provisioned; boots ready |
| Needs internet on the Pi | Yes, unless you stage wheels | No |
| Image is | Reproducible from a script in git | An opaque snapshot |
| Best when | Kits have internet; you want a repeatable build | Kits are offline, or you want them workshop-ready instantly |

If your bench has internet, **start with `build_kit_image.py`** — one command,
nothing to snapshot, and the image is a pure function of this repo:

```bash
python tools/build_kit_image.py --image ~/Downloads/kuiper.img.xz --autoprovision
```

The golden-image route below is the answer when the kits will be **offline**,
or when you want a card that boots straight into a working UI with no
provisioning wait at all. It is also the faster one per card, since nothing is
built on the Pi.

## The golden-image route

Provision one kit, image its card, and flash the rest.

`scripts/provision.sh` stays the source of truth either way: it is the recipe
the golden image is built *from*, and it is how you update a kit already in the
field. The image is a build artifact, not a thing to hand-tweak.

## Why cloned cards need a first-boot step

Ten cards written from one image are ten machines that believe they are the
same machine. Two consequences are not cosmetic:

- **Identical SSH host keys.** Every kit presents the same fingerprint, so
  `known_hosts` cannot tell them apart, and a key lifted off one kit works
  against all ten.
- **Identical `/etc/machine-id`.** systemd-networkd derives its DHCP DUID from
  it, so two kits on one LAN request the same lease and take turns losing it.
  This presents as *"the Pi randomly drops off the network"*, which is a
  miserable thing to debug at a bench with nine other kits running.

`phaser-firstboot.service` fixes both on first boot and then disables itself.
`provision.sh --prepare-image` is what installs and arms it.

## The runbook

### 1. Build the golden kit

Flash stock ADI Kuiper, boot it, and:

```bash
ssh analog@analog.local          # default Kuiper hostname and password
curl -fsSL https://raw.githubusercontent.com/livethisdream/phaser/main/scripts/provision.sh | bash
sudo reboot
```

Then verify it actually works — calibrate it, run a lab, load
`http://phaser.local:8080/`. **Whatever is wrong here is wrong on all ten
cards**, so this is the moment to be fussy.

### 2. Arm it for imaging

Last thing before powering off:

```bash
curl -fsSL https://raw.githubusercontent.com/livethisdream/phaser/main/scripts/provision.sh \
  | bash -s -- --prepare-image --skip-gui --no-reboot
sudo shutdown -h now
```

That installs and enables `phaser-firstboot`, creates `/boot/phaser-hostname`,
and clears the apt cache, journal, and shell history so the image is smaller
and carries no history from the golden kit.

### 3. Image the card

On a Linux box or WSL, with the card in a reader:

```bash
sudo dd if=/dev/sdX of=phaser-golden.img bs=4M status=progress
sudo pishrink.sh phaser-golden.img          # optional, but it shrinks a lot
```

`pishrink` also arms the standard Raspberry Pi rootfs expansion, so each cloned
card grows to fill whatever SD card it lands on. Get `/dev/sdX` wrong and you
overwrite the wrong disk — check with `lsblk` first.

### 4. Flash and name each card

Write `phaser-golden.img` to each card (`dd`, Raspberry Pi Imager, balenaEtcher
— all fine). Then give each one its identity. Either run the prep tool per
card:

```bash
python tools/prep_sdcard.py --hostname phaser-01 --ip 192.168.7.11
python tools/prep_sdcard.py --hostname phaser-02 --ip 192.168.7.12
```

…or, since a golden image already carries `phaser-netalias`, just edit two
plain-text files on the FAT boot partition by hand — Windows and macOS mount it
without any extra tooling:

```
phaser-hostname     phaser-01
phaser-ip           192.168.7.11/24
```

That is the whole per-card step. On first boot each kit regenerates its host
keys and machine-id, takes that hostname and address, reboots once, and comes
up at `http://phaser-01.local:8080/` — and at `http://192.168.7.11:8080/`,
which works even where mDNS does not.

**Give each kit a different address.** Ten cards sharing one alias IP collide
exactly the way ten cards sharing one hostname do.

## Updating kits already in the field

Don't re-image. Re-run the installer, which is idempotent:

```bash
ssh analog@phaser-01.local
curl -fsSL https://raw.githubusercontent.com/livethisdream/phaser/main/install.sh | bash
```

`install.sh` updates the backend and UI. Re-run `provision.sh` instead if what
changed is OS-level — the overlay, the clock units, the Pluto rules.

## If you would rather build the image offline instead

Loop-mounting the `.img` and running the provision steps inside a
`qemu-user-static` chroot is more reproducible than cloning a card: the image
becomes a pure function of the script. It also needs loop devices and
`binfmt_misc` on the build host, which is workable but fiddly under WSL, and
it is a lot of machinery to maintain for ten units. Worth it if you start
doing this across many kits or every Kuiper release; overkill below that.
