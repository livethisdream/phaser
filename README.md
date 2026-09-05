# Phaser Browser-Hosted Runtime

Headless Python backend for the [ADALM-PHASER](https://analogdevicesinc.github.io/documentation/solutions/platforms/phaser/index.html)
8-element X-band phased array, with a browser-based UI for beamforming labs
and CW Doppler radar demos.

The backend runs on the Raspberry Pi that ships with the Phaser kit. A
vanilla-JS + Plotly frontend connects over WebSocket from any machine on
the same network. A local **simulation mode** lets you develop against
physics-based hardware stubs when no Phaser is attached.

**The built UI is committed to this repo and is fully self-contained.**
You do not need Node, npm, or an internet connection to deploy or run it
— see [No-build deployment](#no-build-deployment).

## Quickstart

**A kit that already runs Phaser** -- two lines, from any operating system:

```bash
ssh analog@phaser.local
curl -fsSL https://raw.githubusercontent.com/livethisdream/phaser/main/install.sh | bash
```

Then open `http://phaser.local:8080`. That is install *and* update; re-running
it is how you upgrade. The only thing needed on your own machine is `ssh`; on
Windows that is Settings > Apps > Optional features > OpenSSH Client.

**A brand-new kit** -- flash ADI Kuiper to the card as usual, then prep it on
your laptop before it ever goes in the Pi, so the kit comes up at an address
you already know:

```bash
# 1. Write ADI Kuiper to the card (Raspberry Pi Imager, balenaEtcher, dd)
# 2. Leave the card in the reader and:
python tools/prep_sdcard.py --hostname phaser-01 --ip 192.168.7.11
```

Then put the card in the Pi and power up. It comes up at
`ssh analog@192.168.7.11` (and `phaser-01.local`), no mDNS or DHCP lease
hunting required. Add `--autoprovision` and it installs everything by itself
with no ssh at all. See [Preparing an SD card](#preparing-an-sd-card).

**Already have a reachable kit that needs the Phaser setup** -- run
`provision.sh` on it. Clock, device tree overlay, hostname, PlutoSDR plumbing,
pyadi-iio, then `install.sh`:

```bash
ssh analog@analog.local          # stock Kuiper hostname, before provisioning
curl -fsSL https://raw.githubusercontent.com/livethisdream/phaser/main/scripts/provision.sh | bash
sudo reboot
```

See [Provisioning a new kit](#provisioning-a-new-kit). Setting up more than
one or two kits? Provision one and clone the card --
[docs/golden-image.md](docs/golden-image.md).

**No Phaser attached?** Run sim mode locally instead:

```bash
python phaser_headless.py --sim   # then open http://localhost:8080
```

## Installing and updating

`install.sh` is the only deployment path for the app itself, and it runs **on
the Pi**. There is no laptop-side deploy tool: the script does dependencies,
files, systemd unit and service start in one idempotent pass, on a Pi that has
been provisioned once or one that has been running for a year.

It assumes the OS underneath it is already set up -- overlay loaded, clock
right, pyadi-iio present. On a stock Kuiper card that is not yet true, and
`scripts/provision.sh` is what makes it true; it calls `install.sh` at the end,
so a fresh kit still needs only one command.

It installs only the Python packages that are actually missing, copies the
payload into `/home/analog/pyadi-iio/examples/phaser/`, renders and installs
the systemd unit, then restarts the service and checks it stayed up and the UI
answers 200. It prints both the `.local` name and the IP at the end; if
`phaser.local` does not resolve -- common on Windows without mDNS, or behind
corporate DNS -- use the IP.

Expect **one sudo prompt**, for the systemd unit, and none at all on a re-run
where the unit is already current.

Re-running is how you update, and the idempotency is specific: it compares the
unit's *content* rather than merely checking one exists, so a Pi provisioned by
an older version picks up template changes instead of keeping a stale unit
forever; it replaces the frontend wholesale rather than merging over stale
hashed assets; and it never overwrites an existing `config.py`.

### What lands on the Pi

- The backend entrypoints and their helper modules (`BACKEND_FILES` in
  `install.sh` is the exact list)
- The `LTE*.ftr` AD9361 filter configs, which
  `phaser_find_hb100_headless.py` loads by bare filename at runtime
- `frontend/dist/` and, when present, `frontend-radar/dist/` -- replaced
  wholesale rather than merged, since Vite emits content-hashed filenames and
  copying over the top would accumulate every old build's assets forever
- `config.py`, **only** if the Pi has none

`install.sh` refuses to run if the source has no `frontend/dist/index.html`
at all, rather than installing a backend with no UI in front of it.

### Environment variables

| Variable | Effect |
|---|---|
| `PHASER_REF=<branch-or-tag>` | Install something other than `main` -- how you try a branch before merging it |
| `PHASER_SRC=<dir>` | Install from a local directory instead of downloading |
| `PHASER_WHEELS=<dir>` | Install Python deps from pre-downloaded wheels, with `pip --no-index` |
| `GH_TOKEN` | Authenticate, if the repo is ever made private again |

### Installing a branch you have not pushed

`install.sh` normally fetches a tarball from GitHub, so a branch has to be
pushed before the Pi can see it. To install a working tree instead, copy it
over and point `PHASER_SRC` at it:

```bash
scp -r . analog@phaser.local:/tmp/phaser-src
ssh analog@phaser.local 'PHASER_SRC=/tmp/phaser-src bash /tmp/phaser-src/install.sh'
```

CI rebuilds `frontend/dist/` on every branch, so a pushed branch carries its
own UI and `PHASER_REF=<branch>` installs the right one. Copying an unpushed
tree skips CI, so run `npm run build` first if you changed the frontend.

### Installing with no internet on the Pi

Download on a machine that has internet, carry it over, install from the local
copy. On your laptop:

```bash
curl -fsSL -o phaser.tar.gz https://codeload.github.com/livethisdream/phaser/tar.gz/refs/heads/main
scp phaser.tar.gz analog@phaser.local:/tmp/
```

Then on the Pi:

```bash
ssh analog@phaser.local
mkdir -p /tmp/phaser-src && tar -xzf /tmp/phaser.tar.gz -C /tmp/phaser-src --strip-components=1
PHASER_SRC=/tmp/phaser-src bash /tmp/phaser-src/install.sh
```

`--strip-components=1` matters: GitHub wraps the tree in a
`livethisdream-phaser-<sha>` directory. On Windows use `curl.exe`, not `curl` --
in PowerShell that name is an alias for `Invoke-WebRequest`, which takes
different flags.

That covers everything except the Python packages, which `pip` normally
fetches from the network. For a Pi that has **never** been online, carry those
too -- about 1.2 MB. On your laptop, alongside the tarball:

```bash
pip download --only-binary=:all: \
    --platform linux_armv7l --python-version 39 --implementation cp \
    --index-url https://www.piwheels.org/simple \
    --extra-index-url https://pypi.org/simple \
    -d wheels pyzmq msgpack websockets
scp -r wheels analog@phaser.local:/tmp/
```

```bash
PHASER_SRC=/tmp/phaser-src PHASER_WHEELS=/tmp/wheels bash /tmp/phaser-src/install.sh
```

The `--platform`/`--python-version`/`--implementation` flags matter: they fetch
`cp39` `linux_armv7l` wheels for the Pi rather than wheels for your laptop, and
piwheels is the index that actually has ARM builds of `pyzmq`. With
`PHASER_WHEELS` set, pip runs `--no-index`, so a missing wheel is a clear error
instead of a silent reach for a network that isn't there.

### Why it runs on the Pi

Deliberately: the Pi is the one machine whose environment we control, and every
deployment bug this project has had came from the client side instead. The
header comment in `install.sh` has the full account, and CLAUDE.md records that
a laptop-side deploy tool is not to be reintroduced.

## Preparing an SD card

`tools/prep_sdcard.py` runs on **your laptop**, on a card you have **already
flashed** with stock ADI Kuiper. It writes a few files to the card's FAT boot
partition so the kit comes up reachable at an address you chose before you
plugged it in.

**It does not flash the card, and it refuses a blank one.** Writing the image
is left to Raspberry Pi Imager, balenaEtcher or `dd` — they already do it well,
with a verify pass and guardrails against picking the wrong disk, and none of
that is worth reimplementing badly. So the order is:

```bash
# 1. Flash ADI Kuiper to the card with whatever you normally use
# 2. Leave it in the reader — the boot partition stays mounted — and:
python tools/prep_sdcard.py --hostname phaser-01 --ip 192.168.7.11
# 3. Eject, card into the Pi, power up
```

Standard library only, no root, no dependencies. It finds the card
automatically; `--boot` says where if it cannot, and `--dry-run` shows what it
would write.

| Option | Effect |
| --- | --- |
| `--hostname NAME` | Hostname for this kit (default `phaser`) |
| `--ip ADDR` | Fixed IP, added *alongside* DHCP (default `192.168.7.2/24`; `none` to skip) |
| `--autoprovision [REF]` | Provision unattended on first boot, no ssh needed |
| `--boot PATH` | Where the card is mounted (`E:\`, `/mnt/e`, `/Volumes/boot`) |
| `--boot-mount PATH` | Where the FAT partition mounts **on the Pi**: `/boot` for Kuiper, `/boot/firmware` for bookworm |
| `--dry-run` | Say what would be written, change nothing |

### Why this one laptop-side tool is allowed

This project deliberately has no laptop-side deploy tool -- see
[Why it runs on the Pi](#why-it-runs-on-the-pi). `prep_sdcard.py` is not that.
It runs no logic against the Pi, opens no ssh connection, and touches a card
that has never been in a Pi yet. Every client-side bug that motivated removing
`deploy.py` came from *executing things remotely from Windows*; copying config
onto a FAT filesystem has none of that surface. The line is: **config onto a
card is fine, logic against a running Pi is not.**

It also solves a problem that genuinely cannot be solved from the Pi. A kit is
unreachable until you know its address, and you cannot ssh in to fix that.

### The fixed IP is an alias, not a static configuration

The kit answers on **two** addresses: whatever DHCP gives it, and the one in
`<boot>/phaser-ip`. `phaser-netalias.service` adds the second with
`ip addr add` at every boot.

That matters because it works on **any** network stack -- dhcpcd on bullseye,
NetworkManager on bookworm, systemd-networkd -- without parsing or rewriting
any of their config files. Nothing here breaks when Kuiper changes stacks
underneath us, and a kit on a normal DHCP LAN still behaves completely
normally. The address file lives on the FAT partition so each card in a batch
can be given its own with Notepad; delete the line and you are back to DHCP
only.

### First boot, step by step

`prep_sdcard.py` adds `systemd.run=/boot/firstrun.sh` to `cmdline.txt` -- the
same mechanism Raspberry Pi Imager's own "Advanced options" use. On first boot
`firstrun.sh` sets the hostname, installs the IP alias, strips itself back out
of `cmdline.txt`, and reboots once.

Its **only** job is making the kit reachable. Everything else is `provision.sh`,
over ssh, where you can watch it and it can fail loudly. If `firstrun.sh`
breaks, the kit never appears on the network and there is nothing to ssh into
to debug -- which is also why it logs to `firstrun.log` **on the boot
partition**. If a kit does not show up, pull the card, put it in your laptop,
and read the log.

Two things it is careful about, both of which brick a card:

- **`cmdline.txt` must stay exactly one line.** A stray newline makes the Pi
  refuse to boot with no output at all. The tool backs the file up to
  `cmdline.txt.phaser-orig` first, and a test asserts the round-trip.
- **The hook must disarm itself**, or the Pi runs it on every boot forever.
  `test_firstrun_disarm_restores_the_original_cmdline` applies `firstrun.sh`'s
  own `sed` to what `prep_sdcard.py` wrote and checks it restores the original
  exactly, so the two cannot drift apart.

### Unattended provisioning

`--autoprovision` goes the whole way: the kit installs everything on first
boot with no ssh at all. Give it 20-40 minutes, most of it building pyadi-iio.

It defers the work to a systemd unit that runs after `network-online` rather
than doing it inside the first-boot hook, so it logs to the journal like
anything else and a stall is visible:

```bash
sudo journalctl -u phaser-autoprovision -f
```

It needs passwordless sudo, since nobody is at a keyboard to answer the
prompt. That grant is revoked in both `ExecStartPost` and `ExecStopPost` -- the
latter fires even when provisioning fails, so a kit that dies partway does not
sit on a workshop LAN with NOPASSWD sudo and the stock `analog`/`analog`
password. **Change that password anyway** before any of this goes on a network
you do not control.

## Provisioning a new kit

`scripts/provision.sh` takes a stock ADI Kuiper card to a working Phaser kit.
It runs **on the Pi**, as the `analog` user, and it is idempotent -- re-running
it is how you bring an already-provisioned kit up to date.

```bash
ssh analog@analog.local
curl -fsSL https://raw.githubusercontent.com/livethisdream/phaser/main/scripts/provision.sh | bash
sudo reboot
```

To pass options through the pipe, `bash` needs `-s --`, or it reads them as its
own. This is the form to use when each kit gets its own name:

```bash
curl -fsSL .../scripts/provision.sh | bash -s -- --hostname phaser-01
```

| Option | Effect |
| --- | --- |
| `--hostname NAME` | Name for this kit (default `phaser`) |
| `--timezone TZ` | IANA timezone (default `America/Denver`) |
| `--skip-gui` | Stop after OS setup; do not run `install.sh` |
| `--prepare-image` | Arm the first-boot identity reset and clean the card for imaging |
| `--yes` / `--reboot` / `--no-reboot` | For unattended runs |

`PHASER_SRC`, `PHASER_WHEELS`, `PHASER_REF` and `GH_TOKEN` work the same as in
`install.sh`.

**Give each kit its own hostname.** Ten kits called `phaser` collide on mDNS
and you can only reach one of them.

### What it does, and why not just use the ADI script

The [ADI setup guide](https://analogdevicesinc.github.io/documentation/solutions/platforms/phaser/setup/rpi-setup/)
has you `wget` and run [`phaser_sdcard_setup.sh`](https://github.com/thorenscientific/rpi_setup_stuff).
`provision.sh` does the same jobs, but it is safe to run twice. The upstream
script is not:

- It does `sudo mv /boot/config.txt /boot/config_original.txt` (and the same
  for `/etc/hosts` and `/etc/hostname`), so **a second run overwrites the
  pristine backups with the already-modified files** and the originals are gone.
- It replaces `/boot/config.txt` wholesale with a stock bullseye config,
  silently reverting whatever the running Kuiper image shipped. We merge the
  eight lines that actually matter into a delimited block instead, so there is
  no snapshot to drift and site settings around it survive.
- It `git clone`s pyadi-iio and rebuilds from the tip of `main` **every run** --
  slow, needs a network, and moves a working workshop kit onto whatever landed
  upstream that morning. We check whether `adi.CN0566` imports and only build
  when it does not.
- It assumes `/boot/config.txt`, which moved to `/boot/firmware/` in bookworm.
  We detect both.

The steps, in order:

1. **Clock**, first, because everything after it needs `apt` and TLS.
2. **Base packages** -- an NTP client, `fake-hwclock`, `sshpass`, `git`.
3. **Device tree overlay** merged into `config.txt` (`rpi-cn0566`, heartbeat
   LED, GPIO shutdown pin).
4. **Hostname**, plus the matching `/etc/hosts` line.
5. **PlutoSDR plumbing** -- the udev rule and `iiod` template unit that let a
   Pluto be connected after boot and reconnected freely.
6. **pyadi-iio**, only if `adi.CN0566` does not already import.
7. **`install.sh`**, for the backend and browser UI.

A reboot is required at the end: the overlay and hostname only take effect at
boot, so the Phaser board does not enumerate until you do.

### The clock, specifically

This is the one that bites everybody. A Raspberry Pi has **no RTC**, and stock
Kuiper ships with **no NTP client installed at all** -- not `systemd-timesyncd`,
not `chrony`, not `ntp`:

```
$ systemctl status systemd-timesyncd chrony ntp
Unit systemd-timesyncd.service could not be found.
Unit chrony.service could not be found.
Unit ntp.service could not be found.
```

So `timedatectl set-ntp true` fails with `NTP not supported` -- there is
nothing for systemd to enable. It is not a permissions problem.

That matters more than a wrong timestamp in a log. A wrong clock **breaks TLS
certificate validation and makes `apt` reject its own `Release` files as
not-yet-valid**, so the kit cannot download the very package that would fix the
clock. Hence the ordering: seed from a source that cannot itself be broken by a
wrong clock, *then* install a client, *then* enable it.

`phaser-clock.service` re-runs the check at every boot, in three layers, none
of them fatal:

1. **Floor.** The clock may not predate the script's own mtime. No network
   needed -- this is the only layer that works on a fully offline bench.
2. **HTTP `Date` header, over plain HTTP.** No TLS, so a wrong clock cannot
   break the fetch that fixes the wrong clock. Port 80 also traverses the
   firewalls that commonly drop NTP's UDP/123. This is the bootstrap, not a
   fallback: without it `apt` cannot install an NTP client in the first place.
3. **Real NTP**, if a client is installed and UDP/123 is open. The only layer
   that keeps the clock right over days.

If you just want to fix a kit by hand right now:

```bash
sudo date -s "$(curl -sI http://deb.debian.org/ | sed -n 's/^[Dd]ate: *//p')"
sudo apt update && sudo apt install -y systemd-timesyncd
sudo systemctl enable --now systemd-timesyncd
sudo timedatectl set-ntp true
```

### Setting up more than one kit

Provisioning takes tens of minutes, most of it building pyadi-iio on a Pi.
Provision one kit, image the card, flash the rest --
[docs/golden-image.md](docs/golden-image.md) is the runbook.

Cloned cards need one extra thing, which `--prepare-image` arms: ten cards from
one image share **SSH host keys** (so `known_hosts` cannot tell the kits apart)
and share **`/etc/machine-id`** (so systemd-networkd derives the same DHCP DUID
and two kits fight over one lease, which presents as *"the Pi randomly drops
off the network"*). `phaser-firstboot.service` regenerates both on first boot,
takes the hostname from a plain-text file on the FAT boot partition that
Windows can edit in Notepad, then disables itself.

## No-build deployment

`frontend/dist/` and `frontend-radar/dist/` are **committed to the
repo**, built by GitHub Actions
([`.github/workflows/build-frontends.yml`](.github/workflows/build-frontends.yml)),
which runs on any branch push that touches frontend sources and commits the
result back. CI owns `dist/`; you normally never build it by hand, and you may
need to `git pull` after pushing frontend changes. Building every branch is
deliberate: `PHASER_REF=<branch>` is how a branch gets tried on hardware, and a
branch CI skipped would install a stale UI over a new backend.

Three consequences worth knowing:

**1. Install with no toolchain.** `install.sh` never builds; it ships the
committed `dist/`. A Pi with no Node -- and a laptop with nothing but ssh --
gets a working UI.

**2. A build says which backend it targets.** `frontend/vite.config.js` stamps
`<meta name="phaser-transport" content="web|sim">` into `index.html`. Without
it the two builds are indistinguishable -- Vite folds `VITE_TRANSPORT` away, so
a simulator build and a hardware build have byte-identical HTML and differ only
inside minified JS. See [Two builds, one
tree](#two-builds-one-tree).

**3. The UI is fully offline.** Plotly and the Inter/Outfit webfonts are
vendored into the build rather than pulled from a CDN, so the page
renders on an isolated network with no internet route. The CI job fails
the build if an external `<script src>`, `<link href>`, or CSS `url()`
creeps back in.

Vendoring lives in two places:

- `tools/vendor_plotly.mjs` — the `prebuild` npm hook. Copies
  `plotly.js-dist-min` into `public/vendor/plotly.min.js` at a **stable**
  filename, not a hashed Vite asset, so git stores one blob instead of a
  fresh 3.5 MB one per rebuild. Plotly is pinned to exactly `2.30.0`; the
  UI is tuned against that version.
- `tools/fetch_fonts.py` — refetches the woff2 subsets into
  `frontend/public/fonts/` and mirrors them to `frontend-radar/`. Only
  needed if you change fonts; the files are committed (~180 KB).

## Two builds, one tree

The hosted demo and the Pi run **the same code from the same branch**. They are
not separate branches and not separate apps; they are one source tree built
twice, differing by a single environment variable:

| | `VITE_TRANSPORT` | Output | Published by | Default transport |
|---|---|---|---|---|
| Hardware | unset | `frontend/dist/` (committed) | `install.sh`, to the Pi | WebSocket to `phaser_headless.py` |
| Demo | `sim` | `frontend/dist-pages/` (gitignored) | `deploy-pages.yml`, to Pages | in-browser simulator |

Branching them would be the wrong shape. The simulator is not a variant of the
app -- `transport.js` imports it unconditionally, so it ships *inside* the Pi
build too, which is what makes `?sim=1` work on the Pi when the hardware dies
mid-lab. And `tests/test_sim_parity.py` binds `frontend/src/sim/` to
`phaser_sim.py` sample for sample; separating them would leave the only drift
guard with nothing to compare.

Three things keep the sim default away from hardware, because the failure is
silent -- a simulated sweep looks like a real one, and the only tell is a pill
in the corner:

1. **The builds go to different directories.** `deploy-pages.yml` passes
   `--outDir dist-pages`, so the committed `dist/` is never the demo build.
2. **`vite.config.js` refuses** to write a `VITE_TRANSPORT=sim` build into
   `dist/` at all. Vite's default `outDir` *is* `dist`, so reproducing the
   Pages build locally and forgetting the flag would otherwise overwrite the
   real frontend with a simulator.
3. **The mode is stamped into `index.html`** as
   `<meta name="phaser-transport" content="web|sim">`, and checked three times:
   `build-frontends.yml` refuses to commit a `dist/` that is not `web`,
   `deploy-pages.yml` refuses to publish one that is not `sim`, and `install.sh`
   refuses to install a `sim` build onto a Pi (override with
   `PHASER_ALLOW_SIM_FRONTEND=1`, though `?sim=1` on the URL is the better
   answer for a one-off).

The marker exists because the builds are otherwise indistinguishable: Vite
constant-folds `VITE_TRANSPORT` at build time, leaving byte-identical HTML and
a difference only inside minified JS.

## Architecture

```text
┌─────────────────────┐        ┌───────────────────────────────┐
│  Browser (anywhere) │◄──WS──►│  Raspberry Pi (phaser.local)  │
│  http://…:8080      │◄─HTTP─►│  phaser_headless.py           │
└─────────────────────┘        │    ├─ HTTP  :8080 (frontend)  │
                               │    ├─ WS    :8765 (live data) │
                               │    └─ ZMQ   :5555/5556        │
                               │                               │
                               │  Hardware:                    │
                               │    ADAR1000 (analog steering) │
                               │    ADI Pluto (SDR / IQ)       │
                               │    ADF4159 (LO)               │
                               └───────────────────────────────┘
```

Servers on the Pi:

- **HTTP :8080** — serves `frontend/dist` (main beamforming UI)
- **WebSocket :8765** — browser command channel + live sweep frames
- **ZMQ PUB :5555 / REP :5556** — sweep frames + command channel for local
  scripts; unused by the browser UI
- **HTTP :8081** — CW Doppler radar app (separate `frontend-radar/dist`)

All four ports are overridable (`--http-port`, `--ws-port`, `--pub-port`,
`--rep-port`, `--radar-http-port`).

## Simulation mode (no Phaser required)

There are two simulators, running the same physics. Which one you want
depends on whether you have Python to hand.

| | Runs in | Needs | Use it for |
|---|---|---|---|
| `--sim` | Python, on your machine | a checkout + Python | backend work, calibration flows |
| Simulator Mode | your browser | nothing | demos, frontend work, a dead Pi mid-lab |

### Browser simulator (no install at all)

Turn on **Simulator Mode** in the Configuration pane, or add `?sim=1` to the
URL. The dashboard switches to a simulator that runs entirely in the page — no
backend, no WebSocket, no Python. An orange **SIMULATION** pill sits next to
the connection indicator the whole time, so a synthesized sweep is never
mistakable for a hardware measurement.

This is what makes the hosted demo possible:

**<https://livethisdream.github.io/phaser/>**

That page is the real dashboard, built with `VITE_TRANSPORT=sim`, published by
`.github/workflows/deploy-pages.yml`. Same controls, same plots, same physics —
just synthesized IQ instead of an SDR. It is also useful on the Pi itself: if
the hardware is missing or broken mid-lab, `?sim=1` gets you a working UI
without touching the backend.

The port lives in `frontend/src/sim/`. Python is the source of truth for the
physics; see **Keeping the two simulators in sync** below before changing it.

#### Pointing the page at a different Phaser

By default the frontend talks to whatever origin served it — right when the Pi
serves the page, and wrong when it does not. The **Backend URL** field beside
the Simulator Mode toggle overrides that, saving to `localStorage`
(`?backend=wss://host/ws` also works, and wins over the saved value).

That is what lets the hosted demo drive real hardware: expose the Pi over
Tailscale, put the resulting `wss://` URL in that field, and the page connects
across origins. The backend accepts it — `websockets.serve` is called without
`origins=`, so it does no Origin checking — and a Tailscale hostname carries a
real TLS certificate on 443, so an `https://` page can open the socket without
tripping mixed-content rules. Route `/ws` on that host to port 8765.

> **Note:** `tailscale funnel` publishes to the public internet, and the
> WebSocket server has no authentication. Anyone with the URL can command the
> array. Prefer `tailscale serve`, which stays inside your tailnet.

### Backend simulator (`--sim`)

Run the backend locally against physics-based hardware stubs:

```powershell
# Windows PowerShell
$env:PYTHONIOENCODING = "utf-8"     # once per shell — banner needs UTF-8
python phaser_headless.py --sim
```

```bash
# WSL / Linux / macOS
python phaser_headless.py --sim
```

Then open `http://localhost:8080`. The whole UI works: beam sweeps,
per-element phase, taper presets, Beam Steering, Manual and MVDR digital
beamforming. The sim synthesizes element-level IQ from an HB100 target at
boresight, so the beam patterns are physically consistent — correct beamwidth,
sidelobe roll-off, grating lobes on sparse tapers, MVDR nulls on the
interferer.

`phaser_sim.py` is a development-only module and is not in `BACKEND_FILES`, so
`--sim` is a local thing; on the Pi, use the browser simulator instead.

CW Doppler radar is **not** simulated, in either simulator; it returns
"not available".

### Keeping the two simulators in sync

`frontend/src/sim/` is a JavaScript port of `phaser_sim.py` and
`PhaserHeadless.do_sweep()`. Two implementations of the same physics drift, so
three things stop that:

1. **Constants are generated, not transcribed.** `tools/gen_sim_constants.py`
   writes `frontend/src/sim/constants.generated.js` from `phaser_sim.py`,
   `config.py`, and a real `PhaserHeadless` instance, so the JS defaults are
   the Python defaults by construction. Never edit that file by hand.
2. **`tests/test_sim_parity.py`** runs both implementations over every knob the
   sweep branches on and compares them sample for sample.
3. **CI runs it** (`.github/workflows/tests.yml`), and also fails if the
   generated constants are stale or if the parity tests skip.

So, after changing the simulator physics in Python:

```bash
python tools/gen_sim_constants.py     # if a constant moved
# mirror the change in frontend/src/sim/
pytest tests/test_sim_parity.py       # fails until you do
```

Parity is only as strong as the case matrix in that test. **A new physics knob
needs a new case**, or it is simply untested on the JS side.

### Instructor mode

Append `?instructor=1` to the URL:

```text
http://localhost:8080/?instructor=1
```

This reveals a **Simulator Interferer** panel in the sidebar (a configurable
jammer for MVDR nulling demos). Students loading the app without the flag see
no trace of it, and it stays hidden unless the transport is actually a
simulator — so `?instructor=1` against the *real* Pi app shows nothing either.

It works in the browser simulator too, including on the hosted demo:
`?sim=1&instructor=1`, or just `?instructor=1` on the Pages site, which is
already in simulation.

## Frontend development

Only needed if you're changing the UI. Everyone else can ignore this
section.

```bash
cd frontend
npm install         # one-time
npm run build       # writes to frontend/dist (prebuild hook vendors Plotly)
```

`npm run dev` (Vite hot-reload) can't reach `ws://localhost:8765` — Vite's dev
server doesn't proxy WebSockets — so it has no backend. Open it with `?sim=1`
and the browser simulator drives the UI instead, which makes hot-reload
genuinely useful for frontend work:

```text
http://localhost:5173/?sim=1
http://localhost:5173/?sim=1&instructor=1     # with the interferer panel
```

For the backend-connected path, use `npm run build` + reload, or run
`python phaser_headless.py --sim` and use its HTTP server.

CI rebuilds `dist/` on push and commits it back, on every branch, so leave it
to CI rather than committing build output yourself -- and `git pull` before
your next push.

Backend edits: no build step. Restart `phaser_headless.py` locally, or
re-run `install.sh` on the Pi.

## Calibration files

The Pi keeps calibration state beside `phaser_headless.py`, in a single
JSON store:

- `calibration.json` — HB100 frequency, per-element phase and gain
  corrections, and inter-channel phase corrections. Written atomically,
  and merged rather than replaced, so re-running one calibration does not
  discard the others.

Four **legacy** stores are still read, so a Pi calibrated before the JSON
store keeps working: `hb100_cal.txt`, `phase_cal_val.pkl`,
`gain_cal_val.pkl`, `channel_cal_val.pkl`. Each is superseded the next time
that particular calibration is re-run. Nothing writes them any more.

Loaders in `phaser_functions.py` / `ADAR_pyadi_functions.py` /
`SDR_functions.py` go JSON first, then legacy, then sensible defaults, so a
missing or corrupt store never stops the backend from starting. The UI's
**Calibrate** and **Find HB100** sidebar buttons regenerate all of this;
you don't normally touch it by hand.

Sim mode uses the same loaders and reads whatever cal state is present
locally, so tweaks made on the Pi during development can be scp'd back and
reproduced in sim.

## Codebase map

Top-level Python:

- `phaser_headless.py` — main backend entrypoint (browser-hosted)
- `phaser_sim.py` — physics stubs for `--sim` mode (development only, not
  deployed)
- `phaser_functions.py`, `SDR_functions.py`, `ADAR_pyadi_functions.py`
  — pyadi-iio wrappers + the calibration store (imported by `phaser_headless`)
- `phaser_cw_radar.py` — CW Doppler radar helpers (mode dispatcher +
  frame processing)
- `phaser_cal_headless.py`, `phaser_find_hb100_headless.py`
  — calibration scripts spawned as subprocesses by the backend
- `phaser_service.py` — dead weight from the PyWebView desktop-app era.
  Nothing imports it and `install.sh` does not deploy it; it is kept only
  because parts of `phaser_headless.py` still cite it as the reference for
  hardware quirks
- `config.py` — hardware URIs and default frequencies
- `install.sh` — the installer; runs on the Pi, does deps, files, unit and
  service in one idempotent pass
- `LTE5/10/20_MHz.ftr` — AD9361 filter configs, loaded by bare filename
  relative to the process CWD, so they must sit beside the entrypoints

Frontend (`frontend/`):

- `src/main.js` — all UI logic (Plotly plots, sidebar controls,
  state management, lab presets)
- `src/style.css` — theme, layout
- `src/transport.js` — picks the transport; `transport-web.js` (WebSocket)
  and `transport-sim.js` (in-browser simulator) implement it
- `src/sim/` — the JavaScript physics port
- `index.html` — sidebar structure + accordion sections
- `public/fonts/`, `public/vendor/` — vendored webfonts and Plotly,
  copied verbatim into `dist/` by Vite
- `dist/` — built output, **committed** and deployed as-is

Tooling:

- `tools/prep_sdcard.py` — the one laptop-side tool: writes hostname, fixed IP
  and the first-boot hook onto a flashed card's FAT boot partition. Standard
  library only. Runs no logic against the Pi — see
  [Why this one laptop-side tool is allowed](#why-this-one-laptop-side-tool-is-allowed)
- `tools/vendor_plotly.mjs` — `prebuild` hook, vendors Plotly
- `tools/fetch_fonts.py` — refetch the webfont subsets
- `tools/gen_sim_constants.py` — regenerates the JS simulator constants
- `.github/workflows/build-frontends.yml` — builds both frontends,
  verifies they're self-contained, commits `dist/` back
- `.github/workflows/tests.yml` — pytest, plus the sim-parity guards
- `.github/workflows/deploy-pages.yml` — builds the sim-only Pages demo

`scripts/`:

- `provision.sh` — takes a stock Kuiper card to a working kit (clock, overlay,
  hostname, Pluto plumbing, pyadi-iio), then chains into `install.sh`. Runs on
  the Pi; idempotent. Replaces upstream's `phaser_sdcard_setup.sh`, which is
  not safe to run twice
- `phaser-headless.service.template` — the single definition of the systemd
  unit. `install.sh` renders the `@USER@` / `@INSTALL_DIR@` / `@PYTHON@`
  placeholders from its own constants, which is what keeps the unit's
  `WorkingDirectory` and the install destination from drifting apart
- `build-installer.py` — legacy single-tarball packager, not used by the
  supported install path and not exercised by CI

`scripts/pi/` — files copied verbatim onto the Pi by `provision.sh`; nothing
here is imported by the backend or served to the browser. See
[`scripts/pi/README.md`](scripts/pi/README.md) for provenance.

- `phaser-clock`, `phaser-clock.service` — the clock fix. No RTC on this board
  and no NTP client in stock Kuiper, so a fresh kit boots with a wrong date,
  which breaks TLS and `apt` before anything can install a fix
- `phaser-firstboot`, `phaser-firstboot.service` — per-kit SSH host keys,
  machine-id and hostname for SD cards cloned from a golden image
- `phaser-netalias`, `phaser-netalias.service` — a fixed IP added alongside
  DHCP, read from `<boot>/phaser-ip`. An alias rather than a static config, so
  it works on any network stack and survives Kuiper changing stacks
- `firstrun.sh` — first-boot bootstrap for a stock card, placed on the FAT boot
  partition by `tools/prep_sdcard.py`. Makes the kit reachable and nothing
  else; logs to the boot partition so a failure is readable on your laptop
- `89-pluto.rules`, `iiod-usb@.service` — vendored from
  [thorenscientific/rpi_setup_stuff](https://github.com/thorenscientific/rpi_setup_stuff);
  launch a second `iiod` bound to a PlutoSDR when one is plugged in
- `pluto_update_ad9361.sh` — vendored convenience tool; reflashes an attached
  Pluto to AD9361 2r2t mode. Run by hand, not part of provisioning

`tests/` — pytest suite (`pythonpath = ["."]` in `pyproject.toml` lets it
import the root modules).

`archive/` — superseded design and troubleshooting notes; see
[`archive/README.md`](archive/README.md). Nothing there is current.

`frontend-radar/` — separate frontend for the CW Doppler radar app,
served on port 8081.

## Reference

- [`docs/golden-image.md`](docs/golden-image.md) — runbook for provisioning a
  batch of kits by cloning one card, and why cloned cards need a first-boot
  identity reset
- `docs/2025_Phaser_labs_Python.pdf` — canonical workshop labs
  document, tracked in the repo (`.gitignore` excludes `docs/*.pdf` but
  allowlists this one). Lab presets in the sidebar are aligned to this
  document's initial-state instructions.
- `CLAUDE.md` — project instructions for Claude Code sessions.
