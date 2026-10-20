"""With the hardware missing, the backend must still serve the page and say why.

Found on two bring-ups in a row (2026-10-19, 2026-10-20): the Pluto's USB cable
to the Pi was unplugged, hardware init raised, and the process died before the
HTTP server existed, so the browser said "refused to connect" -- a network
symptom for a cabling fault. HardwareMissingServer serves the UI instead, names
the cable, and restarts the backend once the Pluto appears.
"""

import json
import socket
import sys
import threading
import time
import types
import urllib.request

import pytest


@pytest.fixture(scope="module")
def ph():
    if "adi" not in sys.modules:
        adi = types.ModuleType("adi")
        adi.ad9361 = type("ad9361", (), {})
        adi.adar1000_array = type("adar1000_array", (), {})
        cn0566 = types.ModuleType("adi.cn0566")
        cn0566.CN0566 = type("CN0566", (), {})
        adi.cn0566 = cn0566
        sys.modules["adi"] = adi
        sys.modules["adi.cn0566"] = cn0566
    pytest.importorskip("numpy")
    import phaser_headless
    return phaser_headless


class Pluto:
    """A pluto_check whose answer the test controls."""

    def __init__(self, present):
        self.present = present

    def __call__(self):
        return self.present


def make(ph, present, **kw):
    srv = ph.HardwareMissingServer(OSError("No device found"), pluto_check=Pluto(present), **kw)
    srv._shutdown_permitted = False   # never run sudo in a test
    return srv


def test_unplugged_pluto_names_the_cable(ph):
    st = make(ph, present=False).status()
    assert st["hardware_missing"] is True
    assert st["pluto_found"] is False
    assert "USB cable" in st["message"]
    assert "OSError: No device found" == st["detail"]


def test_present_pluto_gets_the_generic_message(ph):
    st = make(ph, present=True).status()
    assert st["pluto_found"] is True
    assert "did not start" in st["message"]


def test_any_command_answers_with_the_status(ph):
    srv = make(ph, present=False)
    reply = srv.handle_command({"cmd": "get_state"})
    assert reply["status"] == "error" and reply["hardware_missing"] is True


def test_retry_requests_a_restart(ph):
    srv = make(ph, present=False)
    srv.running = True
    assert srv.handle_command({"cmd": "retry_hardware"})["status"] == "ok"
    assert srv.running is False
    assert srv.exit_code == ph.HardwareMissingServer.EXIT_RETRY != 0


def test_watch_fires_once_when_the_pluto_appears(ph):
    srv = make(ph, present=False)
    assert srv.watch_once() is False
    srv.pluto_check.present = True
    assert srv.watch_once() is True
    assert srv.watch_once() is False


def test_no_watch_restart_when_the_pluto_was_there_all_along(ph):
    # Restarting would fail the same way; only Retry restarts.
    assert make(ph, present=True).watch_once() is False


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_serves_the_page_and_exits_nonzero_when_the_pluto_appears(ph, monkeypatch):
    websockets = pytest.importorskip("websockets")
    from websockets.sync.client import connect
    monkeypatch.setattr(ph.HardwareMissingServer, "SETTLE_S", 0.0)
    monkeypatch.setattr(ph.HardwareMissingServer, "POLL_S", 0.05)
    http_port, ws_port = _free_port(), _free_port()
    srv = make(ph, present=False, http_port=http_port, ws_port=ws_port)
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("code", srv.run()), daemon=True)
    t.start()

    deadline = time.time() + 5
    while True:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{http_port}/", timeout=1) as r:
                assert r.status == 200
            break
        except OSError:
            if time.time() > deadline:
                raise
            time.sleep(0.05)

    with connect(f"ws://127.0.0.1:{ws_port}") as ws:
        first = json.loads(ws.recv(timeout=2))
        assert first["type"] == "hardware_missing"
        assert first["data"]["pluto_found"] is False
        ws.send(json.dumps({"cmd": "get_state", "id": "req_1"}))
        reply = json.loads(ws.recv(timeout=2))
        assert reply["id"] == "req_1" and reply["hardware_missing"] is True

    srv.pluto_check.present = True
    t.join(timeout=5)
    assert result.get("code") == ph.HardwareMissingServer.EXIT_RETRY
