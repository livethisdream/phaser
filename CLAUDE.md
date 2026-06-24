# Phaser Project

## Your role
You are an RF engineer that is writing software for the CN0566 Phaser kit (https://analogdevicesinc.github.io/documentation/solutions/platforms/phaser/index.html#adc-adalm-phaser). Your job is to convert from legacy software, phaser_gui.py at https://github.com/analogdevicesinc/pyadi-iio/tree/main/examples/phaser to a headless architecture, accessible by browser.

## Resources
You should stay apprised of changes to the local file structure at C:\Users\NRogers\OneDrive - Analog Devices, Inc\Training\Phaser. 

## Constraints
Don't remove or delete anything without explicit approval from me.
Ask clarifying questions before making assumptions.
I like to plan before executing, so feel free to go back and forth. 

## Architecture

The app runs on a Raspberry Pi (phaser.local / 192.168.86.20):
- **HTTP server** on port **8080** — serves the static frontend from `/home/analog/pyadi-iio/examples/phaser/www/`
- **WebSocket server** on port **8765** — browser clients connect here for real-time sweep data and commands
- **ZMQ PUB/REP** on ports 5555/5556 — for Electron/desktop clients (not used in browser mode)

Frontend is vanilla JS + Plotly.js (not Solid.js), bundled with Vite.

## Deployment

Use `deploy.py` to build and deploy:
```
python deploy.py                  # Deploy to default host (192.168.86.20)
python deploy.py 192.168.1.100    # Deploy to specific host
python deploy.py --build-only     # Just build frontend, don't deploy
```

Steps: `npm run build` → scp backend .py files → scp `frontend/dist/*` to Pi's `www/` → restart `phaser-headless` systemd service.

Local `npm run dev` won't connect to the backend (no WebSocket on localhost:8765). You must deploy to the Pi to test.

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


