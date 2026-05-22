#!/usr/bin/env python3
"""NDJSON IPC worker around BackendService.

This process reads one JSON message per stdin line and writes one JSON response
per stdout line. It is transport-only glue so the service layer remains shared.
"""

import argparse
import json
import signal
import sys
from contextlib import redirect_stdout
from typing import Any, Dict

from phaser_service import BackendService, default_serializer

IPC_VERSION = "1.0"


def parse_runtime_args():
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--sim", action="store_true", help="Run backend in simulation mode")
    return parser.parse_args()


def make_error_response(req_id: str, cmd: str, message: str, code: str, details: Dict[str, Any] | None = None):
    return {
        "id": req_id,
        "type": "response",
        "cmd": cmd,
        "status": "error",
        "message": message,
        "error": {"code": code, "details": details or {}},
        "version": IPC_VERSION,
    }


def make_ok_response(req_id: str, cmd: str, data: Dict[str, Any] | list | str | None = None, message: str | None = None):
    payload = {
        "id": req_id,
        "type": "response",
        "cmd": cmd,
        "status": "ok",
        "data": data if data is not None else {},
        "version": IPC_VERSION,
    }
    if message is not None:
        payload["message"] = message
    return payload


def write_message(msg: Dict[str, Any]):
    sys.stdout.write(json.dumps(msg, default=default_serializer) + "\n")
    sys.stdout.flush()


def validate_request(raw: Dict[str, Any]):
    req_id = raw.get("id")
    msg_type = raw.get("type")
    cmd = raw.get("cmd")
    if not req_id:
        return None, None, make_error_response("", "", "Missing request id", "E_BAD_REQUEST")
    if msg_type != "request":
        return req_id, cmd or "", make_error_response(req_id, cmd or "", "Message type must be 'request'", "E_BAD_REQUEST")
    if not cmd:
        return req_id, "", make_error_response(req_id, "", "Missing command", "E_BAD_REQUEST")
    return req_id, cmd, None


def handle_request(service: BackendService, raw: Dict[str, Any]):
    req_id, cmd, err = validate_request(raw)
    if err is not None:
        return err

    data = raw.get("data") or {}
    try:
        if cmd == "sweep":
            state = data.get("state") if isinstance(data, dict) else {}
            if state is None:
                state = {}
            with redirect_stdout(sys.stderr):
                result = service.process_sweep(state)
            return make_ok_response(req_id, cmd, result)

        if cmd == "get_state":
            with redirect_stdout(sys.stderr):
                result = service.get_ui_state()
            if result.get("status") == "ok":
                return make_ok_response(req_id, cmd, result.get("data", {}))
            return make_error_response(req_id, cmd, result.get("message", "Unknown error"), "E_INTERNAL")

        if cmd == "get_lab":
            lab_idx = data.get("lab_idx") if isinstance(data, dict) else None
            if lab_idx is None:
                return make_error_response(req_id, cmd, "Missing lab_idx", "E_BAD_REQUEST")
            with redirect_stdout(sys.stderr):
                result = service.get_lab_preset(int(lab_idx))
            if result.get("status") == "ok":
                return make_ok_response(req_id, cmd, result.get("data", {}))
            return make_error_response(req_id, cmd, result.get("message", "Unknown error"), "E_BAD_LAB_INDEX")

        if cmd == "run_calibration":
            task_name = data.get("task_name") if isinstance(data, dict) else None
            if not task_name:
                return make_error_response(req_id, cmd, "Missing task_name", "E_BAD_REQUEST")
            with redirect_stdout(sys.stderr):
                result = service.run_calibration(task_name)
            if result.get("status") == "ok":
                return make_ok_response(req_id, cmd, {}, result.get("message"))
            code = "E_SIM_UNSUPPORTED" if "sim mode" in str(result.get("message", "")).lower() else "E_INTERNAL"
            return make_error_response(req_id, cmd, result.get("message", "Unknown error"), code)

        if cmd == "get_cal_status":
            with redirect_stdout(sys.stderr):
                result = service.get_calibration_status()
            if result.get("status") == "ok":
                return make_ok_response(req_id, cmd, result.get("data", {}))
            return make_error_response(req_id, cmd, result.get("message", "Unknown error"), "E_INTERNAL")

        return make_error_response(req_id, cmd, f"Unknown command {cmd}", "E_UNKNOWN_CMD")
    except Exception as exc:
        return make_error_response(req_id, cmd, str(exc), "E_INTERNAL")


def main():
    args = parse_runtime_args()
    service = BackendService(sim_mode=args.sim)

    def _shutdown_handler(signum, frame):  # noqa: ARG001
        try:
            service.shutdown()
        finally:
            sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)

    with redirect_stdout(sys.stderr):
        service.startup()
    write_message({"type": "event", "cmd": "worker_ready", "data": {"sim_mode": args.sim}, "version": IPC_VERSION})

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                write_message(make_error_response("", "", f"Invalid JSON: {exc}", "E_BAD_REQUEST"))
                continue

            response = handle_request(service, raw)
            write_message(response)
    finally:
        with redirect_stdout(sys.stderr):
            service.shutdown()


if __name__ == "__main__":
    main()

