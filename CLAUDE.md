# Phaser Project

## Your role
You are an RF engineer that is writing software for the CN0566 Phaser kit (https://analogdevicesinc.github.io/documentation/solutions/platforms/phaser/index.html#adc-adalm-phaser). Your job is to convert from legacy software, phaser_gui.py at https://github.com/analogdevicesinc/pyadi-iio/tree/main/examples/phaser to a headless architecture, accessible by browser.

## Resources
You should stay apprised of changes to the local file structure at `~/projects/phaser` (WSL).
The project moved here from the OneDrive-hosted copy on 2026-08-25; that copy is stale, do not edit it.

Reference materials:

- `docs/2025_Phaser_labs_Python.pdf` — canonical PHASER Phased Array Radar Workshop labs document (2025 edition). **Tracked in the repo**: `.gitignore` excludes `docs/*.pdf` but allowlists this one. Source of truth for the Lab 1–9 preset audit and for verifying what each lab's default state should be.

## Repo layout

Root holds only what has to be there: the backend entrypoints and the helper
modules they import (these are scp'd flat into the Pi's working directory and
resolve each other by bare import, so they cannot move), the `LTE*.ftr` filter
configs that pyadi-iio loads by bare filename, `install.sh`, and the usual
project metadata.

- `scripts/` — setup/provisioning and the legacy installer packager
- `tests/` — pytest suite
- `archive/` — superseded notes, nothing current
- `docs/` — reference material

## Constraints
Don't remove or delete anything without explicit approval from me.
Ask clarifying questions before making assumptions.
I like to plan before executing, so feel free to go back and forth.

## Open todos

At the start of every session, read the latest open todos from auto-memory before doing anything else:

```
~/.claude/projects/-home-nrogers-projects-phaser/memory/project_phaser_todos.md
```

That file is the source of truth for what's pending across sessions — open beamforming/radar work items, branch hygiene reminders, and follow-ups the user has flagged. Surface what's open at the top of the conversation so we can pick where we left off.

## Architecture

The app runs on a Raspberry Pi (phaser.local / 192.168.86.20):
- **HTTP server** on port **8080** — serves the static frontend from `/home/analog/pyadi-iio/examples/phaser/www/`
- **WebSocket server** on port **8765** — browser clients connect here for real-time sweep data and commands
- **ZMQ PUB/REP** on ports 5555/5556 — for Electron/desktop clients (not used in browser mode)

Frontend is vanilla JS + Plotly.js (not Solid.js), bundled with Vite.

## Deployment

**`install.sh` is the only deployment path.** It runs ON the Pi, not on a
laptop:

```
ssh analog@phaser.local
curl -fsSL https://raw.githubusercontent.com/livethisdream/phaser/main/install.sh | bash
```

It fetches the repo tarball from GitHub (`PHASER_REF` selects a branch or tag,
default `main`), installs missing Python deps, copies `BACKEND_FILES` and the
committed `frontend/dist/` into
`/home/analog/pyadi-iio/examples/phaser/`, renders and enables the unit from
`scripts/phaser-headless.service.template`, restarts the service and checks
the UI answers 200. Idempotent — re-running it is how you update.

`PHASER_SRC=/path/to/repo` installs from a local copy instead of downloading,
which is how you test an unpushed branch. `PHASER_WHEELS` adds offline pip.

There is deliberately no laptop-side deploy tool. `deploy.py` and
`scripts/setup*.sh` used to be one, and every deployment bug this project had
came from the client side — cmd.exe globbing, PATHEXT, no ControlMaster on
Windows OpenSSH, `ssh -t` versus sudo, a Microsoft Store alias masquerading as
`python`. Moving the logic onto the Pi means the client needs nothing but ssh.
Do not reintroduce one.

`frontend/dist/` and `frontend-radar/dist/` are **committed** (built by
`.github/workflows/build-frontends.yml`), so install.sh never needs Node.
It refuses to run if `frontend/dist/index.html` is absent from the source.

The built UI is fully offline-capable: Plotly (pinned 2.30.0) is vendored by
the `prebuild` hook `tools/vendor_plotly.mjs` into `public/vendor/` at a stable
filename, and Inter/Outfit woff2 subsets live in `public/fonts/`
(`tools/fetch_fonts.py` refetches them). No CDN, no Google Fonts. CI fails the
build if an external `<script src>`, `<link href>`, or CSS `url()` reappears.

Local `npm run dev` has no backend (no WebSocket on localhost:8765), but
`http://localhost:5173/?sim=1` runs the browser simulator, so hot-reload is
usable for frontend work. Otherwise install to the Pi, or run
`python phaser_headless.py --sim`.

## Two simulators

`phaser_sim.py` (Python, `--sim`) and `frontend/src/sim/` (JavaScript, in the
browser) run the same physics. The JS port is what makes the GitHub Pages demo
possible -- Pages is static-only, so there is no Python and no WebSocket there.

**Python is the source of truth.** Before changing simulator physics:

1. Change it in Python.
2. `python tools/gen_sim_constants.py` if a constant moved. Never hand-edit
   `frontend/src/sim/constants.generated.js`.
3. Mirror the change in `frontend/src/sim/`.
4. `pytest tests/test_sim_parity.py` -- it fails until you do.

Parity is only as strong as the case matrix in `tests/test_sim_parity.py`.
**Adding a physics knob means adding a case there**, or it goes untested on the
JS side.

Two traps the port already accounts for, both load-bearing:

- `do_sweep()` and `ConvertPhaseToSteerAngle()` use a truncated `2 * 3.14159`,
  not real pi (legacy compatibility), while `phaser_sim`'s wave synthesis uses
  real `np.pi`. Both are in the generated constants as `STEER_PI` and `SIM_PI`.
  Do not "fix" either.
- `array.js` uses `pyRound()`, not `Math.round`: Python rounds ties to even and
  JS rounds them up.

## GitHub Pages demo

`.github/workflows/deploy-pages.yml` builds `frontend/` with
`VITE_TRANSPORT=sim` and publishes to <https://livethisdream.github.io/phaser/>.

That build goes to `frontend/dist-pages/` and is **gitignored**. The committed
`frontend/dist/` is the one `install.sh` ships to the Pi and must keep
defaulting to the real backend -- keeping them separate is what stops the sim
default reaching hardware.

## Codebase Knowledge Graph

A knowledge graph of this codebase exists at `graphify-out/graph.json`. When answering architecture questions, tracing data flows, or exploring how components connect, run:

```
/graphify query "<your question>"
```

Key entry points (god nodes):
- **PhaserHeadless** — headless runtime, bridges WebSocket commands to hardware
- **BackendService** — orchestrates calibration, sweep processing, hardware lifecycle
- **PhaserServer** — real hardware interface (vs PhaserServerSim for simulation)
- **ADI Phaser Dashboard** — frontend UI entry point

Communities:
- Phaser Headless Runtime (WebSocket server, command loop)
- Frontend Dashboard UI (vanilla JS + Plotly.js controls, plots)
- Backend Service Layer (calibration tasks, state management)
- Hardware Control (ADAR/SDR pyadi-iio functions)


