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
configs that pyadi-iio loads by bare filename, `deploy.py`, and the usual
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

`frontend/dist/` and `frontend-radar/dist/` are **committed** (built by
`.github/workflows/build-frontends.yml`), so `deploy.py` does NOT build by
default — a clone with only Python + ssh can deploy. Building is opt-in.

```
python deploy.py                  # Deploy committed build to phaser.local
python deploy.py 192.168.1.100    # Deploy to specific host
python deploy.py --radar          # Also deploy the CW radar app
python deploy.py --sim-only       # Prepare for --sim, don't deploy
python deploy.py --build          # Rebuild from source first (needs Node)
python deploy.py --build-only     # Rebuild, don't deploy
```

Steps: (optional build) -> scp backend .py files -> scp `frontend/dist/*` to
the Pi -> check provisioning -> restart `phaser-headless` systemd service. If
the committed build is missing entirely, deploy.py builds it when npm is
available and errors with guidance when it isn't.

The provisioning check is what makes a fresh Pi work: deploy.py verifies the
runtime imports and installs + enables the unit from
`scripts/phaser-headless.service.template` when the Pi has none. That template
is the only definition of the unit — `setup-pi.sh` used to carry a second,
drift-prone heredoc copy and no longer does. deploy.py now exits non-zero when
the restart fails, instead of printing "Deployment complete!" over a service
that never started. It warns (advisory only) when sources look newer than
the committed build.

The CW radar frontend (`frontend-radar/`, served on :8081) is **opt-in** via
`--radar`. `--no-radar` is still accepted as a no-op.

The built UI is fully offline-capable: Plotly (pinned 2.30.0) is vendored by
the `prebuild` hook `tools/vendor_plotly.mjs` into `public/vendor/` at a stable
filename, and Inter/Outfit woff2 subsets live in `public/fonts/`
(`tools/fetch_fonts.py` refetches them). No CDN, no Google Fonts. CI fails the
build if an external `<script src>`, `<link href>`, or CSS `url()` reappears.

Local `npm run dev` won't connect to the backend (no WebSocket on
localhost:8765). Deploy to the Pi to test, or run `python phaser_headless.py
--sim`.

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


