#!/usr/bin/env python3
import argparse
import asyncio
import json
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import uvicorn

from phaser_service import BackendService, default_serializer


def parse_runtime_args():
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--sim", action="store_true", help="Run backend in simulation mode")
    args, _ = parser.parse_known_args()
    return args


RUNTIME_ARGS = parse_runtime_args()

# ---------------- FastAPI Configuration ----------------

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

service = BackendService(sim_mode=RUNTIME_ARGS.sim)


@app.get("/api/state")
def get_ui_state():
    return service.get_ui_state()


@app.get("/api/lab/{lab_idx}")
def get_lab_preset(lab_idx: int):
    return service.get_lab_preset(lab_idx)


@app.get("/api/calibration/status")
def get_calibration_status():
    return service.get_calibration_status()


@app.post("/api/calibration/{task_name}")
def run_calibration(task_name: str):
    return service.run_calibration(task_name)


@app.on_event("startup")
def startup_event():
    service.startup()


@app.on_event("shutdown")
def shutdown_event():
    service.shutdown()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    print("New GUI WebSocket Client connected")

    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                cmd = msg.get("cmd", "")

                if cmd == "sweep":
                    state_msg = msg.get("state", {})
                    # Process sweep in a background thread to prevent pausing async IO.
                    result = await asyncio.to_thread(service.process_sweep, state_msg)
                    reply = {"status": "ok", "data": result}
                    json_str = json.dumps(reply, default=default_serializer)
                    await websocket.send_text(json_str)
                else:
                    await websocket.send_text(json.dumps({"status": "error", "message": f"Unknown command {cmd}"}))

            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({"status": "error", "message": "Invalid JSON mapping"}))
            except Exception as e:
                print(f"Error handling websocket message: {e}")
                await websocket.send_text(json.dumps({"status": "error", "message": str(e)}))

    except WebSocketDisconnect:
        print("GUI WebSocket Client disconnected")


# Mount static site at root
static_dir = os.path.join(os.path.dirname(__file__), "frontend", "dist")
if os.path.exists(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    print(f"Mounted Static Web Application at {static_dir}")
else:
    print(f"Warning: Static directory {static_dir} not found. Ensure 'web_app' is present here.")

if __name__ == "__main__":
    # Ensure this binds to all interfaces (0.0.0.0) so it's accessible over the network.
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
