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

That is the whole thing -- install *and* update. `install.sh` is idempotent,
so re-running it is how you pick up a new version. It needs nothing on your
machine but `ssh`, which every OS ships.

**No Phaser attached?** Run sim mode locally instead:

```bash
python phaser_headless.py --sim   # then open http://localhost:8080
```

### About the installer

Nothing is needed locally beyond `ssh` -- no Python, no clone, no toolchain.

Expect **one sudo prompt**, for the systemd unit. You are at an interactive
shell, so it just asks.

It is idempotent -- run it again to update. It updates a drifted systemd unit,
replaces the frontend atomically rather than merging over stale hashed assets,
never overwrites an existing `config.py`, and installs only the Python packages
that are actually missing. It finishes by checking the service stayed up and
the UI answers 200, printing both the `.local` name and the IP.

If `phaser.local` does not resolve -- common on Windows without mDNS, or behind
corporate DNS -- use the Pi's IP address instead.

`install.sh` runs **on the Pi** deliberately. The Pi is the one machine whose
environment we control; every deployment bug this project has had came from the
client side instead (cmd.exe globbing, PATHEXT, no ControlMaster on Windows
OpenSSH, `ssh -t` versus sudo, a Microsoft Store alias masquerading as
`python`). None of that is about installing Phaser. The header comment in
`install.sh` has the full account.

### Installing without internet on the Pi

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
too. On your laptop, alongside the tarball:

```bash
pip download --only-binary=:all: \
    --platform linux_armv7l --python-version 39 --implementation cp \
    --index-url https://www.piwheels.org/simple \
    --extra-index-url https://pypi.org/simple \
    -d wheels pyzmq msgpack websockets
scp -r wheels analog@phaser.local:/tmp/
```

Then add `PHASER_WHEELS` to the install:

```bash
PHASER_SRC=/tmp/phaser-src PHASER_WHEELS=/tmp/wheels bash /tmp/phaser-src/install.sh
```

About 1.2 MB in total. The `--platform`/`--python-version`/`--implementation`
flags matter: they fetch `cp39` `linux_armv7l` wheels for the Pi rather than
wheels for your laptop, and piwheels is the index that actually has ARM builds
of `pyzmq`. With `PHASER_WHEELS` set, pip runs `--no-index`, so a missing wheel
is a clear error instead of a silent reach for a network that isn't there.

### Other options

`PHASER_REF=<branch-or-tag>` installs something other than `main`.
`GH_TOKEN` authenticates if the repo is ever made private again.

### Installing your own working tree

`install.sh` normally fetches a tarball from GitHub, so a branch has to be
pushed before the Pi can see it. To install a tree you have not pushed, copy it
to the Pi and point `PHASER_SRC` at it:

```bash
scp -r . analog@phaser.local:/tmp/phaser-src
ssh analog@phaser.local 'PHASER_SRC=/tmp/phaser-src bash /tmp/phaser-src/install.sh'
```

### Prerequisites

An ssh client. On Windows that is
Settings > Apps > Optional features > OpenSSH Client.

**On Windows, do not rely on `python` already being on PATH.** Windows ships a
Microsoft Store *App Execution Alias* at
`...\AppData\Local\Microsoft\WindowsApps\python.exe`, which `where.exe python`
happily finds and which is not an interpreter -- `python --version` fails on it.
Install a real one from python.org (tick "Add python.exe to PATH"), or, if you
have `uv`, `uv python install`.

### First-time Pi provisioning

There is no separate provisioning step. `install.sh` does the whole job on a
Pi straight out of the box: it installs any missing Python dependencies,
places the files, renders and enables the systemd unit from
`scripts/phaser-headless.service.template`, starts the service and checks the
UI answers on :8080.

It compares the unit's *content* rather than merely checking one exists, so a
Pi provisioned by an older version picks up template changes instead of
keeping a stale unit forever. `config.py` is never overwritten -- a Pi's copy
may hold site-specific URIs and calibration.

Expect **one** sudo prompt, for the systemd unit -- and none at all on a
re-run where the unit is already current. You are sitting at an interactive
shell on the Pi, so sudo simply asks the way it always does.

## No-build deployment

`frontend/dist/` and `frontend-radar/dist/` are **committed to the
repo**, built by GitHub Actions
([`.github/workflows/build-frontends.yml`](.github/workflows/build-frontends.yml))
on every push that touches frontend sources. CI owns `dist/`; you
normally never build it by hand.

Two consequences worth knowing:

**1. Install with no toolchain.** `install.sh` never builds; it ships the
committed `dist/`. A Pi with no Node -- and a laptop with nothing but ssh --
gets a working UI.

**2. The UI is fully offline.** Plotly and the Inter/Outfit webfonts are
vendored into the build rather than pulled from a CDN, so the page
renders on an isolated network with no internet route. The CI job fails
the build if an external `<script src>`, `<link href>`, or CSS `url()`
creeps back in.

Vendoring lives in two places:

- `tools/vendor_plotly.mjs` — copies `plotly.js-dist-min` out of
  `node_modules` into `public/vendor/plotly.min.js` at a **stable**
  filename (not a hashed Vite asset, so git stores one blob instead of
  a fresh 3.5 MB one per rebuild). Wired as the `prebuild` npm hook, so
  `npm run build` picks it up automatically. Plotly is pinned to exactly
  `2.30.0` — the UI is tuned against that version.
- `tools/fetch_fonts.py` — refetches the woff2 files into
  `frontend/public/fonts/` and mirrors them to `frontend-radar/`. Only
  needed if you change fonts; the files are committed (~180 KB, latin +
  latin-ext subsets only).

`install.sh` refuses to run if the source has no `frontend/dist/index.html`
at all, rather than installing a backend with no UI in front of it.

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

## Updating a Pi

Re-run the installer. It is idempotent, so this is both the install path and
the update path:

```bash
ssh analog@phaser.local
curl -fsSL https://raw.githubusercontent.com/livethisdream/phaser/main/install.sh | bash
```

`PHASER_REF=<branch-or-tag>` installs something other than `main`, which is
how you try a branch before merging it.

It installs:

- The backend entrypoints and their helper modules (`BACKEND_FILES` in
  `install.sh` is the exact list)
- The `LTE*.ftr` AD9361 filter configs, which
  `phaser_find_hb100_headless.py` loads by bare filename at runtime
- `frontend/dist/` and, when present, `frontend-radar/dist/` -- replaced
  wholesale rather than merged, since Vite emits content-hashed filenames and
  copying over the top would accumulate every old build's assets forever
- `config.py`, **only** if the Pi has none

## Simulation mode (no Phaser required)

There are two simulators, running the same physics. Which one you want
depends on whether you have Python to hand.

| | Runs in | Needs | Use it for |
|---|---|---|---|
| `--sim` | Python, on your machine | a checkout + Python | backend work, calibration flows |
| Sim button | your browser | nothing | demos, frontend work, a dead Pi mid-lab |

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
the Simulator Mode toggle overrides that (`?backend=wss://host/ws` also works,
and wins over the saved value).

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
per-element phase, taper presets, Beam Steering, Manual and MVDR
digital beamforming. The sim synthesizes element-level IQ from an
HB100 target at boresight, so the resulting beam patterns are
physically consistent (correct beamwidth, sidelobe roll-off, grating
lobes on sparse tapers, MVDR nulls on the interferer).

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

This reveals a **Simulator Interferer** panel in the sidebar (a
configurable jammer for MVDR nulling demos). Students loading the app
without this flag see no trace of the interferer controls.

The panel is also hidden if the backend isn't in sim mode, so a
student loading the *real* Pi app with `?instructor=1` still doesn't
see it.

This works in the browser simulator too, including on the hosted demo —
`?sim=1&instructor=1`, or just `?instructor=1` on the Pages site, which is
already in simulation.

## Frontend development

Only needed if you're changing the UI. Everyone else can ignore this
section — CI builds `dist/` on push.

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

You can commit `dist/` yourself or let CI rebuild it on push; CI is
authoritative and will overwrite with its own build either way.

Backend edits: no build step. Restart `phaser_headless.py` locally, or
re-run `install.sh` on the Pi.

## Calibration files

The Pi keeps calibration state in these files at the same directory as
`phaser_headless.py`:

- `hb100_cal.txt` — HB100 signal frequency (single float, Hz)
- `phase_cal_val.pkl` — per-element phase corrections (8 floats, deg)
- `gain_cal_val.pkl` — per-element gain corrections (8 floats)
- `channel_cal_val.pkl` — inter-channel phase corrections (2 floats)

Loaders in `phaser_functions.py` / `ADAR_pyadi_functions.py` /
`SDR_functions.py` fall back to sensible defaults if a file is
missing. The GUI's **Calibrate** and **Find HB100** sidebar buttons
regenerate these; you don't normally touch them by hand.

Sim mode uses the same loaders and reads whatever cal files are
present locally, so tweaks made on the Pi during development can be
scp'd back and reproduced in sim.

## Codebase map

Top-level Python:

- `phaser_headless.py` — main backend entrypoint (browser-hosted)
- `phaser_sim.py` — physics stubs for `--sim` mode
- `phaser_functions.py`, `SDR_functions.py`, `ADAR_pyadi_functions.py`
  — pyadi-iio wrappers + cal loaders (imported by `phaser_headless`)
- `phaser_cw_radar.py` — CW Doppler radar helpers (mode dispatcher +
  frame processing)
- `phaser_cal_headless.py`, `phaser_find_hb100_headless.py`
  — calibration scripts spawned as subprocesses by the backend
- `phaser_service.py` — legacy desktop-app service layer (older
  PyWebView path; still used by the release bundle)
- `config.py` — hardware URIs and default frequencies
- `install.sh` — the installer; runs on the Pi, does deps, files, unit and
  service in one idempotent pass
- `LTE5/10/20_MHz.ftr` — AD9361 filter configs, loaded by bare filename
  relative to the process CWD, so they must sit beside the entrypoints

Frontend (`frontend/`):

- `src/main.js` — all UI logic (Plotly plots, sidebar controls,
  state management, lab presets)
- `src/style.css` — theme, layout
- `src/transport.js` — WebSocket transport facade
- `index.html` — sidebar structure + accordion sections
- `public/fonts/`, `public/vendor/` — vendored webfonts and Plotly,
  copied verbatim into `dist/` by Vite
- `dist/` — built output, **committed** and deployed as-is

Tooling:

- `tools/vendor_plotly.mjs` — `prebuild` hook, vendors Plotly
- `tools/fetch_fonts.py` — refetch the webfont subsets
- `.github/workflows/build-frontends.yml` — builds both frontends,
  verifies they're self-contained, commits `dist/` back

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

## Reference

- `docs/2025_Phaser_labs_Python.pdf` — canonical workshop labs
  document, tracked in the repo (`.gitignore` excludes `docs/*.pdf` but
  allowlists this one). Lab presets in the sidebar are aligned to this
  document's initial-state instructions.
- `graphify-out/graph.json` — knowledge graph of the codebase for the
  `/graphify` slash command.
- `CLAUDE.md` — project instructions for Claude Code sessions.

## Sub-directories

- `release/PhaserBundle/` — self-contained laptop-hosted variant
  (older desktop-app architecture, PyWebView). Has its own README and
  install scripts.
- `frontend-radar/` — separate frontend for the CW Doppler radar app
  (served on port 8081).
