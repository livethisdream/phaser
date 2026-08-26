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

From a fresh clone, with your Phaser kit on the same network:

```bash
python deploy.py                  # default host: phaser.local
python deploy.py 192.168.1.42     # or an explicit IP
```

Then open `http://phaser.local:8080`.

**Prerequisites**: Python 3.11+ and an OpenSSH client on your machine,
passwordless ssh to `analog@<pi>` (`ssh-copy-id analog@<pi>` if not),
and a Pi that has already been provisioned (below). No Node required.

**No Phaser attached?** Run sim mode locally instead:

```bash
python phaser_headless.py --sim   # then open http://localhost:8080
```

### Prerequisites

`deploy.py` imports **only the standard library**, so any Python 3.9+ runs it
with nothing installed -- no venv, no `uv sync`, no pip. A test enforces that
(`tests/test_deploy_deps.py`), because the alternative is telling a tester to
install a toolchain before they can deploy anything.

You also need an ssh client. On Windows that is
Settings > Apps > Optional features > OpenSSH Client.

**On Windows, do not rely on `python` already being on PATH.** Windows ships a
Microsoft Store *App Execution Alias* at
`...\AppData\Local\Microsoft\WindowsApps\python.exe`, which `where.exe python`
happily finds and which is not an interpreter -- `python --version` fails on it.
Install a real one from python.org (tick "Add python.exe to PATH"), or, if you
have `uv`, `uv python install`.

### First-time Pi provisioning

A Pi straight out of the box needs its Python deps installed once.
`scripts/setup.sh` / `scripts/setup.ps1` do that, then deploy. Like
`deploy.py`, they use the committed build and need no Node:

```powershell
.\scripts\setup.ps1               # Windows PowerShell
.\scripts\setup.ps1 192.168.1.42
.\scripts\setup.ps1 -Build        # force a frontend rebuild (needs Node)
```

```bash
./scripts/setup.sh                # macOS / Linux / WSL
./scripts/setup.sh 192.168.1.42
./scripts/setup.sh --build        # force a frontend rebuild (needs Node)
```

They can be run from anywhere; both anchor themselves to the repo root.

After that, `python deploy.py` is all you need for every subsequent
update.

The systemd unit is **not** part of this step. `deploy.py` owns it: before
restarting, it checks for `/etc/systemd/system/phaser-headless.service` and,
if the Pi has none, renders `scripts/phaser-headless.service.template` and
installs + enables it. So a deploy to a never-provisioned Pi ends with a
running service instead of a `WARN:` line under a "Deployment complete!"
banner. `deploy.py` also verifies `pyzmq`, `msgpack` and `websockets` are
importable by `analog` through `/usr/bin/python3` — the exact user and
interpreter the unit runs as — and fails with a pointer to `setup.sh` when
they aren't, rather than leaving you a crash-looping service.

Expect **one** sudo prompt on the Pi. First-time provisioning installs,
enables and starts the unit in a single `ssh -t` session, because sudo's
credential timestamp is per-tty and a second session would prompt again. A
redeploy to an already-provisioned Pi prompts once too, for the restart.
`ssh-copy-id analog@<host>` removes the separate *ssh* password prompt; on
macOS/Linux/WSL a single shared connection is used for the whole deploy, so
even without a key you are asked once rather than once per file. (Windows
OpenSSH has no `ControlMaster`, so PowerShell users on a password-only Pi are
prompted per copy — copy a key over to avoid it.)

## No-build deployment

`frontend/dist/` and `frontend-radar/dist/` are **committed to the
repo**, built by GitHub Actions
([`.github/workflows/build-frontends.yml`](.github/workflows/build-frontends.yml))
on every push that touches frontend sources. CI owns `dist/`; you
normally never build it by hand.

Two consequences worth knowing:

**1. Clone → deploy, with no toolchain.** `deploy.py` does not build by
default. A machine with only Python and ssh can deploy a working UI.
Building is opt-in via `--build`.

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

`deploy.py` warns if your frontend sources look newer than the committed
build. That's advisory only — mtimes are unreliable across clones and
OneDrive sync — but it stops a stale `dist/` from shipping silently.

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
- **ZMQ PUB :5555 / REP :5556** — for legacy desktop/Electron clients
- **HTTP :8081** — CW Doppler radar app (separate `frontend-radar/dist`)

## Deploying to the Pi

```bash
python deploy.py                  # deploy committed build to phaser.local
python deploy.py 192.168.1.42     # deploy to a specific host
python deploy.py --radar          # also deploy the CW radar app
python deploy.py --sim-only       # prepare for --sim, don't deploy
python deploy.py --build          # rebuild from source first (needs Node)
python deploy.py --build-only     # rebuild, don't deploy
```

The CW radar frontend is **opt-in** via `--radar`. It's a separate app on
:8081 with no simulation path, so it isn't deployed unless you ask for
it. (`--no-radar` is still accepted, and is now a no-op.)

If the committed build is missing entirely, `deploy.py` builds it for you
when npm is available, and tells you what to do when it isn't.

`deploy.py` copies:

- Backend Python entrypoints and their helper modules (see the file for
  the exact list)
- The `LTE*.ftr` AD9361 filter configs, which
  `phaser_find_hb100_headless.py` loads by bare filename at runtime
- Built frontend (`frontend/dist/*`)
- Radar frontend (`frontend-radar/dist/*`) with `--radar`

It deliberately **does not overwrite** `config.py`, so any Pi-specific
values (URIs, calibrated defaults) survive. It does seed one when the Pi has
none at all — a from-scratch install dir is otherwise an immediate
`import config` crash loop, since `phaser_headless.py` exits at module level
without it. It installs the systemd unit if absent, then restarts
`phaser-headless.service` over ssh.

## Simulation mode (no Phaser required)

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

CW Doppler radar is **not** simulated; it returns "not available in
--sim mode".

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

## Frontend development

Only needed if you're changing the UI. Everyone else can ignore this
section — CI builds `dist/` on push.

```bash
cd frontend
npm install         # one-time
npm run build       # writes to frontend/dist (prebuild hook vendors Plotly)
```

`npm run dev` (Vite hot-reload) is **not** wired to the backend — it
can't reach `ws://localhost:8765` because Vite's dev server doesn't
proxy WebSockets. Use `npm run build` + reload the browser, or run
`python phaser_headless.py --sim` and use its HTTP server.

You can commit `dist/` yourself or let CI rebuild it on push; CI is
authoritative and will overwrite with its own build either way.

Backend edits: no build step. Restart `phaser_headless.py` (locally or
on the Pi via `deploy.py`).

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
- `deploy.py` — scp + restart-service workflow (build is opt-in)
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

- `setup.sh`, `setup.ps1` — first-time Pi provisioning, then deploy
- `setup-pi.sh` — the Pi-side half, piped over ssh by the two above.
  Installs the Python deps and the install dir. It does **not** write the
  systemd unit; `deploy.py` does
- `phaser-headless.service.template` — the single definition of the systemd
  unit. `deploy.py` renders the `@USER@` / `@INSTALL_DIR@` / `@PYTHON@`
  placeholders from its own constants, which is what keeps the unit's
  `WorkingDirectory` and the scp destination from drifting apart
- `build-installer.py` — legacy single-tarball packager, not used by the
  supported setup/deploy path and not exercised by CI

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
