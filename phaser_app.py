#!/usr/bin/env python3
"""
phaser_app.py — PyWebView desktop launcher
==========================================
Single entry-point that opens a native window and connects it directly to the
Phaser backend service without a web server.

Usage:
    python phaser_app.py           # real hardware
    python phaser_app.py --sim     # simulation mode

Cross-platform:
    Windows  — WebView2 (Edge Chromium, pre-installed on Win 10/11)
    Linux    — WebKitGTK  (requires: libwebkit2gtk-4.0  or  libwebkit2gtk-4.1)
                 Ubuntu/Debian: sudo apt install python3-gi gir1.2-webkit2-4.0
                 Fedora/RHEL:   sudo dnf install webkit2gtk4.0
    macOS    — WKWebView (built-in)

Architecture:
    phaser_app.py
        ├── PhaserAPI     <- JS-to-Python bridge exposed as window.pywebview.api
        │       └── BackendService  (phaser_service.py)
        └── webview.Window  <- loads frontend/dist/index.html
"""

import sys
import argparse
import json
import os
import threading
from pathlib import Path

# ---------------------------------------------------------------------------
# Argument parsing (must happen before service import so --sim reaches config)
# ---------------------------------------------------------------------------

def _parse_args():
    parser = argparse.ArgumentParser(description="ADI Phaser Desktop App")
    parser.add_argument("--sim", action="store_true", help="Run in simulation mode (no hardware)")
    parser.add_argument("--debug", action="store_true", help="Enable WebView devtools")
    return parser.parse_args()


ARGS = _parse_args()

# ---------------------------------------------------------------------------
# Backend service
# ---------------------------------------------------------------------------

from phaser_service import BackendService

_service: BackendService | None = None
_service_lock = threading.Lock()
_window = None
_window_lock = threading.Lock()
_window_state = {
    "maximized": False,
}


def _get_service() -> BackendService:
    global _service
    with _service_lock:
        if _service is None:
            _service = BackendService(sim_mode=ARGS.sim)
            _service.startup()
        assert _service is not None
    return _service


# ---------------------------------------------------------------------------
# JS-facing API — every public method becomes window.pywebview.api.<name>()
#
# PyWebView calls these from a thread pool so they must be thread-safe.
# Return values are serialised to JSON automatically by pywebview.
# ---------------------------------------------------------------------------

class PhaserAPI:
    """Bridge between JavaScript and BackendService.

    Method names mirror the IPC command surface in PHASE3_IPC_SPEC.md so the
    same frontend transport-ipc.js adapter routes calls here identically to
    how it would talk to a Tauri sidecar.

    PyWebView exposes these as: window.pywebview.api.<method>(payload)
    The JS bridge adapter translates: bridge.invoke(cmd, data) ->
        window.pywebview.api.invoke(JSON.stringify({cmd, data}))
    and parses the returned JSON string.
    """

    # ------------------------------------------------------------------
    # Single dispatch entry — all IPC commands come through invoke()
    # ------------------------------------------------------------------

    def invoke(self, envelope: str) -> str:
        """Receive an NDJSON envelope string, route to the right handler,
        return a JSON-encoded response envelope string."""
        try:
            msg = json.loads(envelope)
        except Exception as exc:
            return json.dumps({
                "status": "error",
                "message": f"Bad request envelope: {exc}",
                "error": {"code": "E_BAD_REQUEST"},
            })

        cmd = msg.get("cmd", "")
        data = msg.get("data", {})
        req_id = msg.get("id", "")

        try:
            result = self._dispatch(cmd, data)
        except Exception as exc:
            result = {
                "status": "error",
                "message": str(exc),
                "error": {"code": "E_INTERNAL"},
            }

        result["id"] = req_id
        result.setdefault("version", "1.0")
        return json.dumps(result, default=_json_default)

    def move_window(self, dx: int, dy: int) -> str:
        """Move the window by a pixel delta. Called rapidly during JS custom drag."""
        with _window_lock:
            if _window is None:
                return json.dumps({"status": "error", "message": "Window unavailable"})
            try:
                _window.move(int(_window.x + dx), int(_window.y + dy))
                return json.dumps({"status": "ok"})
            except Exception as exc:
                return json.dumps({"status": "error", "message": str(exc)})

    def window_control(self, action: str) -> str:
        """Handle native desktop window actions for the custom title bar."""
        with _window_lock:
            if _window is None:
                return json.dumps({
                    "status": "error",
                    "message": "Desktop window is not ready",
                    "error": {"code": "E_WINDOW_UNAVAILABLE"},
                })

            try:
                if action == "minimize":
                    _window.minimize()
                elif action == "toggle_maximize":
                    if _window_state["maximized"]:
                        _window.restore()
                    else:
                        _window.maximize()
                    _window_state["maximized"] = not _window_state["maximized"]
                elif action == "close":
                    _window.destroy()
                else:
                    return json.dumps({
                        "status": "error",
                        "message": f"Unknown window action: {action}",
                        "error": {"code": "E_UNKNOWN_WINDOW_ACTION"},
                    })
            except Exception as exc:
                return json.dumps({
                    "status": "error",
                    "message": str(exc),
                    "error": {"code": "E_WINDOW_CONTROL"},
                })

            return json.dumps({
                "status": "ok",
                "data": {
                    "maximized": _window_state["maximized"],
                },
                "version": "1.0",
            })

    # ------------------------------------------------------------------
    # Command routing
    # ------------------------------------------------------------------

    def _dispatch(self, cmd: str, data: dict) -> dict:
        svc = _get_service()
        if cmd == "sweep":
            return {"status": "ok", "data": svc.process_sweep(data.get("state", {}))}

        if cmd == "get_state":
            return svc.get_ui_state()

        if cmd == "get_lab":
            lab_idx = int(data.get("lab_idx", 0))
            return svc.get_lab_preset(lab_idx)

        if cmd == "run_calibration":
            task_name = data.get("task_name", "")
            return svc.run_calibration(task_name)

        if cmd == "get_cal_status":
            return svc.get_calibration_status()

        return {
            "status": "error",
            "message": f"Unknown command: {cmd}",
            "error": {"code": "E_UNKNOWN_CMD"},
        }


def _json_default(obj):
    """Fallback serialiser for numpy types etc."""
    import numpy as np
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    return str(obj)


# ---------------------------------------------------------------------------
# Frontend path resolution
# ---------------------------------------------------------------------------

def _find_frontend() -> Path:
    root = Path(__file__).resolve().parent
    dist = root / "frontend" / "dist" / "index.html"
    if dist.exists():
        return dist
    # Dev fallback: Vite dev server (not ideal for desktop, but helpful)
    dev = root / "frontend" / "index.html"
    if dev.exists():
        print("WARNING: Using dev frontend (no build). Run 'npm run build' in frontend/ for production.")
        return dev
    raise FileNotFoundError(
        "Frontend not found. Run 'npm run build' inside frontend/ first.\n"
        f"Expected: {dist}"
    )


# ---------------------------------------------------------------------------
# Startup initialisation happens in a background thread so the window
# can open immediately with a loading indicator while hardware warms up.
# ---------------------------------------------------------------------------

def _init_service_async():
    """Pre-warm the service in a background thread.
    Any exception is caught; the UI will show a disconnected state.
    """
    try:
        _get_service()
        print("Backend service ready.")
    except Exception as exc:
        print(f"Backend init failed: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    webview = None
    try:
        import webview  # noqa: PLC0415
    except ImportError:
        print(
            "pywebview is required for the desktop launcher.\n"
            "  pip install pywebview\n"
            "Linux additionally requires: libwebkit2gtk-4.0",
            file=sys.stderr,
        )
        sys.exit(1)

    frontend_path = _find_frontend()
    api = PhaserAPI()

    # Restrict drag initiation to direct drag-region targets to avoid tiny
    # window position jumps when clicking nested titlebar elements.
    webview.settings['DRAG_REGION_DIRECT_TARGET_ONLY'] = True

    # Build the URL for the window — pywebview expects a file:// URL or a path
    frontend_url = frontend_path.as_uri()

    mode_label = "Simulation" if ARGS.sim else "Hardware"
    global _window
    with _window_lock:
        _window_state["maximized"] = False

    window = webview.create_window(
        title=f"ADI Phaser Dashboard [{mode_label}]",
        url=frontend_url,
        js_api=api,
        width=1400,
        height=900,
        min_size=(900, 600),
        background_color="#0f172a",   # matches --bg-base in dark theme
        frameless=True,
        easy_drag=True,
    )

    with _window_lock:
        _window = window

    # Expose desktop runtime flags and the IPC bridge once the page is ready.
    def _on_loaded():
        js = """
        (function() {
            window.__PHASER_DESKTOP = true;
            window.__PHASER_TRANSPORT = 'ipc';
            // Install a pywebview bridge shim that matches the __PHASER_IPC_BRIDGE API.
            // window.pywebview.api.invoke() is already available; we wrap it here.
            if (!window.__PHASER_IPC_BRIDGE && window.pywebview) {
                window.__PHASER_IPC_BRIDGE = {
                    invoke: async function(cmd, data) {
                        const envelope = JSON.stringify({ cmd: cmd, data: data || {}, version: '1.0' });
                        const result = await window.pywebview.api.invoke(envelope);
                        try { return JSON.parse(result); }
                        catch(e) { return { status: 'error', message: 'Bridge parse error: ' + e }; }
                    }
                };
                console.info('[PHASER APP] PyWebView IPC bridge installed');
            }
            window.dispatchEvent(new Event('phaserdesktopready'));
        })();
        """
        window.evaluate_js(js)

    window.events.loaded += _on_loaded

    # Pre-warm backend in background while window is opening
    init_thread = threading.Thread(target=_init_service_async, daemon=True)
    init_thread.start()

    webview.start(debug=ARGS.debug)

    # Cleanup after window closes
    global _service
    with _service_lock:
        if _service is not None:
            try:
                _service.shutdown()
            except Exception as exc:
                print(f"Shutdown warning: {exc}", file=sys.stderr)
            _service = None

    with _window_lock:
        _window = None
        _window_state["maximized"] = False


if __name__ == "__main__":
    main()

