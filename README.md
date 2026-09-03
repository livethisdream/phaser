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

Two lines, from any operating system:

```bash
ssh analog@phaser.local
curl -fsSL https://raw.githubusercontent.com/livethisdream/phaser/main/install.sh | bash
```

Then open `http://phaser.local:8080`.

That is the whole thing -- install, provision *and* update. The only thing
needed on your own machine is `ssh`; on Windows that is
Settings > Apps > Optional features > OpenSSH Client.

**No Phaser attached?** Run sim mode locally instead:

```bash
python phaser_headless.py --sim   # then open http://localhost:8080
```

## Installing and updating

`install.sh` is the only deployment path, and it runs **on the Pi**. There is
no separate provisioning step and no laptop-side deploy tool: the script does
dependencies, files, systemd unit and service start in one idempotent pass, on
a Pi straight out of the box or on one that has been running for a year.

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

### CTF mode

`?ctf=1` reveals a **CTF Mode** panel for the GRCon26 signals CTF. A player
steers the beam through a sector sequence, holding each sector briefly, and the
backend hands back a flag when the sequence completes.

```text
http://phaser.local:8080/?ctf=1
```

Unlike instructor mode, the URL parameter is UI convenience, **not** a secret.
`frontend/dist` is served to every browser that connects and a CTF player's
whole job is to go looking, so the sequence check and the flag both live in
`phaser_ctf.py` on the backend. The panel only renders what the backend
reports.

**Neither the flag nor the target sequence is in this repo.** Both are read
from the environment, or from `ctf_flag.txt` / `ctf_sequence.txt` next to the
backend. Both are gitignored, and `install.sh` cannot ship them: its
`BACKEND_FILES` is an allowlist, so anything not named there stays on the Pi by
construction. Without them the module falls back to a harmless demo sequence
and a placeholder flag, so the panel is still developable.

```bash
export PHASER_CTF_SEQUENCE="3 1 4 1 2"
export PHASER_CTF_FLAG="flag{...}"
export PHASER_CTF_ALLOW_SIM=0        # optional: hardware-only flag issue
python phaser_headless.py
```

**Under systemd, exporting in your shell is not enough.** The service starts
from the unit, not from your login environment, so a backend run by
`phaser-headless.service` would still be on the placeholder. The unit reads
`/etc/default/phaser-ctf` if it exists (`EnvironmentFile=-`, so a Pi that is
not running the CTF is unaffected). Write it once on the Pi, after installing:

```bash
sudo install -m 600 -o root -g root /dev/null /etc/default/phaser-ctf
sudo tee /etc/default/phaser-ctf >/dev/null <<'EOF'
PHASER_CTF_SEQUENCE=3 1 4 1 2
PHASER_CTF_FLAG=flag{...}
PHASER_CTF_ALLOW_SIM=0
EOF
sudo systemctl restart phaser-headless
```

Mode `0600` and root ownership matter: `systemctl show phaser-headless` prints
every `Environment=` value to any user, whereas values loaded from an
`EnvironmentFile` are read by PID 1 at start and never echoed back. Do not put
the flag in the unit itself.

No sudo on the machine? The sidecar files are the alternative, and need no
privileges -- `ctf_flag.txt` and `ctf_sequence.txt` go in the install directory
next to `phaser_ctf.py` (`/home/analog/pyadi-iio/examples/phaser/`), owned by
the service user. The environment wins when both are present.

Sectors default to five bins centred at -60/-30/0/+30/+60 degrees with +/-12
degrees tolerance and a 2 s dwell; all of that is constructor arguments on
`CtfMode`. The mode is passive -- it watches the commanded `phaseList` and
answers `ctf_status` / `ctf_reset`, and does nothing at all unless a browser
asks. `pytest tests/test_phaser_ctf.py` covers the state machine with no
hardware and no backend.

**It needs the Python backend.** The browser simulator has no CTF: putting the
flag in a JS bundle would defeat the point of keeping it server-side, so
`?ctf=1` under `?sim=1` (or on the hosted demo) shows the panel explaining it
needs a real backend rather than a challenge you cannot win.

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

- `tools/vendor_plotly.mjs` — `prebuild` hook, vendors Plotly
- `tools/fetch_fonts.py` — refetch the webfont subsets
- `tools/gen_sim_constants.py` — regenerates the JS simulator constants
- `.github/workflows/build-frontends.yml` — builds both frontends,
  verifies they're self-contained, commits `dist/` back
- `.github/workflows/tests.yml` — pytest, plus the sim-parity guards
- `.github/workflows/deploy-pages.yml` — builds the sim-only Pages demo

`scripts/`:

- `phaser-headless.service.template` — the single definition of the systemd
  unit. `install.sh` renders the `@USER@` / `@INSTALL_DIR@` / `@PYTHON@`
  placeholders from its own constants, which is what keeps the unit's
  `WorkingDirectory` and the install destination from drifting apart
- `build-installer.py` — legacy single-tarball packager, not used by the
  supported install path and not exercised by CI

`tests/` — pytest suite (`pythonpath = ["."]` in `pyproject.toml` lets it
import the root modules).

`archive/` — superseded design and troubleshooting notes; see
[`archive/README.md`](archive/README.md). Nothing there is current.

`frontend-radar/` — separate frontend for the CW Doppler radar app,
served on port 8081.

## Reference

- `docs/2025_Phaser_labs_Python.pdf` — canonical workshop labs
  document, tracked in the repo (`.gitignore` excludes `docs/*.pdf` but
  allowlists this one). Lab presets in the sidebar are aligned to this
  document's initial-state instructions.
- `CLAUDE.md` — project instructions for Claude Code sessions.
