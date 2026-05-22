# Phaser Laptop-Hosted Runtime

This project can run entirely on a laptop while controlling remote Phaser hardware over pyadi-iio.

## URI configuration

**New to network setup?** See [NETWORK_SETUP_GUIDE.md](NETWORK_SETUP_GUIDE.md) for step-by-step diagnostics.

Set hardware URIs in `config.py`:

```python
uri_mode = "prefer_config"  # auto | prefer_config | custom
rpi_uri = "ip:phaser.local"
sdr_uri = "ip:phaser.local:50901"
```

Override per-run without editing files:

- `PHASER_RPI_URI`
- `PHASER_SDR_URI`

## Overview of Installation Process

1. Install Python dependencies (default: `pip` via `requirements.txt`).
2. Build frontend static assets (`frontend/dist`).
3. Run `phaser_server.py` on the laptop.
4. Open `http://<laptop-ip>:8000` in a browser on the network.

## Automated scripts (Windows PowerShell)

The project includes bundled deployment scripts under `scripts/`.

### Install (default: pip + requirements.txt)

```powershell
cd "C:\path\to\Phaser"
.scripts\install.ps1
```

Optional `uv` installer mode:

```powershell
cd "C:\path\to\Phaser"
.scripts\install.ps1 -Installer uv
```

### Start server

Hardware mode:

```powershell
cd "C:\path\to\Phaser"
.scripts\start-real.ps1
```

Simulation mode:

```powershell
cd "C:\path\to\Phaser"
.scripts\start-sim.ps1
```

### Build distributable bundle

```powershell
cd "C:\path\to\Phaser"
.scripts\build-release-bundle.ps1
```

Output:

- `release/PhaserBundle/`
- `release/PhaserBundle.zip`

The bundle includes the backend files, `frontend/dist`, runtime scripts, and any existing calibration files.

Calibration persistence is JSON-first:

- Writes go to `calibration.json`.
- Legacy files (`hb100_cal.txt`, `*_cal_val.pkl`) are still read as fallback if JSON keys are missing.

### Calibration file format

The canonical calibration file is `calibration.json` in the repo root.

```json
{
  "version": 1,
  "updated_at": 1714583000.0,
  "hb100_freq_hz": 10525000000.0,
  "phase_cal": [0.0, 0.2, -0.1, 0.0, 0.1, -0.2, 0.0, 0.0],
  "gain_cal": [1.0, 1.01, 0.99, 1.0, 1.0, 1.02, 0.98, 1.0],
  "channel_cal": [0.0, -0.4]
}
```

Notes:

- JSON writes are performed by `phaser_functions.py` save helpers.
- Legacy files are read only as migration fallback.
- After successful `find_hb100` or `phaser_cal`, backend runtime calibration is reloaded and the UI state is refreshed automatically.

### GUI calibration workflow

From the app sidebar, use:

- `Calibrate Phaser` -> runs `phaser_cal.py`
- `Find HB100` -> runs `phaser_find_hb100.py`

No manual script launch is required for normal operation. The calibration status panel updates while the task runs, and successful completion auto-refreshes GUI state.

`phaser_cal.py` is non-interactive by default for GUI use. To re-enable calibration plots when running manually, set:

```powershell
$env:PHASER_CAL_PLOT = "1"
uv run python phaser_cal.py
```

## Run modes

### Desktop app (Phase 3 — recommended)

No browser required. A native window is opened via PyWebView; the Python backend
runs in-process using direct IPC (no web server).

```powershell
# Windows — simulation
.\scripts\start-app-sim.ps1

# Windows — real hardware
.\scripts\start-app-real.ps1
```

```bash
# Linux / macOS — simulation
bash scripts/start-app-sim.sh

# Linux / macOS — real hardware
bash scripts/start-app-real.sh
```

**Linux system prerequisite** (one-time):
```bash
# Ubuntu/Debian
sudo apt install python3-gi gir1.2-webkit2-4.0

# Fedora/RHEL
sudo dnf install webkit2gtk4.0

# Arch
sudo pacman -S webkit2gtk
```

### Web server mode (legacy / network access)

Still fully supported if you need browser access from another machine:

```powershell
# Windows
.\scripts\start-real.ps1    # hardware
.\scripts\start-sim.ps1     # simulation
```

```bash
# Linux / macOS
bash scripts/start-real.sh
bash scripts/start-sim.sh
```

Or directly:
```
uv run python phaser_server.py --sim
```

## Notes

- **Desktop app** (`phaser_app.py`) opens a native window; no port is bound.
- **Web server** (`phaser_server.py`) still serves `/ws` and static `/` for browser access.
- Backend logic lives in `phaser_service.py` — shared by both entry points.
- `phaser_find_hb100.py` resolves URIs the same way as the server.
- Frontend websocket URL defaults to same host, overridable via `window.__PHASER_WS_URL` or `VITE_WS_URL`.

## Phase 3 IPC Worker (experimental)

A local NDJSON IPC worker is now available for Phase 3 transport testing:

- Worker entrypoint: `phaser_ipc_worker.py`
- Command contract: `release/PHASE3_IPC_SPEC.md`

Run worker in simulation mode:

```powershell
cd "C:\path\to\Phaser"
.\scripts\start-ipc-worker-sim.ps1
```

Run worker in real hardware mode:

```powershell
cd "C:\path\to\Phaser"
.\scripts\start-ipc-worker-real.ps1
```

Run the IPC smoke test harness:

```powershell
cd "C:\path\to\Phaser"
.\scripts\test-ipc-worker.ps1
```

## Frontend transport selection (Phase 3)

The frontend transport facade in `frontend/src/transport.js` supports two modes:

- `web` (default): REST + WebSocket via `transport-web.js`
- `ipc`: desktop bridge via `transport-ipc.js`

Enable IPC mode at runtime by setting:

```html
<script>
  window.__PHASER_TRANSPORT = 'ipc';
  window.__PHASER_IPC_BRIDGE = {
    invoke: (cmd, data) => Promise.resolve({ status: 'error', message: 'not wired' })
  };
</script>
```

You can also set `VITE_TRANSPORT=ipc` at build time.

Enable browser-only IPC mock testing (no desktop shell required):

```powershell
cd "C:\path\to\Phaser\frontend"
npm run dev
```

Then open:

- `http://localhost:5173/?mockIpc=1`

This auto-installs a mock bridge from `frontend/src/ipc-bridge-mock.js` and forces IPC transport mode for contract testing.

