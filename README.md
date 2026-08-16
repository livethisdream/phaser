# Phaser Browser-Hosted Runtime

Headless Python backend for the [ADALM-PHASER](https://analogdevicesinc.github.io/documentation/solutions/platforms/phaser/index.html)
8-element X-band phased array, with a browser-based UI for beamforming labs
and CW Doppler radar demos.

The backend runs on the Raspberry Pi that ships with the Phaser kit. A
vanilla-JS + Plotly frontend connects over WebSocket from any machine on
the same network. A local **simulation mode** lets you develop against
physics-based hardware stubs when no Phaser is attached.

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

Edit files locally, then run one command to build the frontend, scp the
Python files, and restart the systemd service:

```bash
python deploy.py                  # deploys to phaser.local (default)
python deploy.py 192.168.1.42     # deploys to a specific host
python deploy.py --build-only     # just runs `npm run build`
python deploy.py --no-radar       # skips the CW radar frontend
```

The Pi runs `phaser-headless.service`; `deploy.py` restarts it over ssh
after the scp. Open `http://phaser.local:8080` in a browser and you're
there.

`deploy.py` copies:

- Backend Python entrypoints and their helper modules (see the file for
  the exact list)
- Built frontend (`frontend/dist/*` → `www/`)
- Radar frontend (`frontend-radar/dist/*`) if present

It deliberately **does not** copy `config.py`, so any Pi-specific values
(URIs, calibrated defaults) survive.

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

### CTF mode

`?ctf=1` reveals a **CTF Mode** panel for the GRCon26 signals CTF. A
player steers the beam through a sector sequence, holding each sector
briefly, and the backend hands back a flag when the sequence completes.

```text
http://phaser.local:8080/?ctf=1
```

Unlike instructor mode, the URL parameter is UI convenience, **not** a
secret. `frontend/dist` is served to every browser that connects and a
CTF player's whole job is to go looking, so the sequence check and the
flag both live in `phaser_ctf.py` on the backend. The panel only
renders what the backend reports.

**Neither the flag nor the target sequence is in this repo.** Both are
read from the environment, or from `ctf_flag.txt` / `ctf_sequence.txt`
next to the backend (both gitignored, and `deploy.py` does not copy
them — same treatment as `config.py`). Without them the module falls
back to a harmless demo sequence and a placeholder flag, so the panel
is still developable.

```bash
export PHASER_CTF_SEQUENCE="3 1 4 1 2"
export PHASER_CTF_FLAG="flag{...}"
export PHASER_CTF_ALLOW_SIM=0        # optional: hardware-only flag issue
python phaser_headless.py
```

Sectors default to five bins centred at −60/−30/0/+30/+60° with ±12°
tolerance and a 2 s dwell; all of that is constructor arguments on
`CtfMode`. The mode is passive — it watches the commanded `phaseList`
and answers `ctf_status` / `ctf_reset`, and does nothing at all unless
a browser asks. `python test_phaser_ctf.py` covers the state machine
with no hardware and no backend.

## Local development

```bash
# One-time
cd frontend
npm install

# Iterating on frontend
cd frontend
npm run build       # writes to frontend/dist
```

`npm run dev` (Vite hot-reload) is **not** wired to the backend — it
can't reach `ws://localhost:8765` because Vite's dev server doesn't
proxy WebSockets to the backend. Use `npm run build` + reload the
browser, or run `python phaser_headless.py --sim` and use its HTTP
server.

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
- `deploy.py` — build + scp + restart-service workflow

Frontend (`frontend/`):

- `src/main.js` — all UI logic (Plotly plots, sidebar controls,
  state management, lab presets)
- `src/style.css` — theme, layout
- `src/transport.js` — WebSocket transport facade
- `index.html` — sidebar structure + accordion sections
- `dist/` — built output (deployed as-is; committed for the Pi)

## Reference

- `docs/2025_Phaser_labs_Python.pdf` — canonical workshop labs
  document (gitignored, kept locally). Lab presets in the sidebar are
  aligned to this document's initial-state instructions.
- `graphify-out/graph.json` — knowledge graph of the codebase for the
  `/graphify` slash command.
- `CLAUDE.md` — project instructions for Claude Code sessions.

## Sub-directories

- `release/PhaserBundle/` — self-contained laptop-hosted variant
  (older desktop-app architecture, PyWebView). Has its own README and
  install scripts.
- `frontend-radar/` — separate frontend for the CW Doppler radar app
  (served on port 8081).
