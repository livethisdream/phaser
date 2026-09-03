# Files installed onto the Pi by `scripts/provision.sh`

Everything here is copied verbatim onto a Phaser Pi. Nothing in this directory
is imported by the backend or served to the browser.

## Provenance

`89-pluto.rules` and `iiod-usb@.service` are vendored from
<https://github.com/thorenscientific/rpi_setup_stuff/tree/main/phaser>, which is
what the ADI setup guide tells you to `wget`. They are copied rather than
fetched at run time so a provision works offline, so the exact bytes that land
on ten kits are the same bytes, and so a change upstream shows up as a diff in
this repo rather than as a kit that behaves differently from its siblings.

Both are small and have not needed to change: the udev rule matches a USB
product ID and the unit runs one binary. Re-check them against upstream when a
new Kuiper release lands.

What is deliberately **not** vendored is upstream's `config_phaser.txt`. That
file is ~95% a stock bullseye `config.txt` with about eight Phaser-specific
lines at the end, and the upstream script installs it by *replacing*
`/boot/config.txt` wholesale -- which silently reverts whatever the running
Kuiper image shipped. `provision.sh` merges just those eight lines into the
image's own config instead, inside a marker block. There is nothing to drift
from because there is no copy.

`pluto_update_ad9361.sh` is vendored for the same reasons and is a convenience
tool, not part of provisioning: it reflashes an attached PlutoSDR to AD9361
2r2t mode. `provision.sh` drops it in the analog user's home; you run it by
hand when a Pluto needs it.

## Written here, not vendored

- `phaser-clock` / `phaser-clock.service` -- the clock fix. This board has no
  RTC and Kuiper ships with no NTP client at all, so a fresh kit boots with a
  wrong date, which makes TLS and `apt` fail before anything can install a fix.
- `phaser-firstboot` / `phaser-firstboot.service` -- per-kit identity for
  cloned SD cards. See `docs/golden-image.md`.
