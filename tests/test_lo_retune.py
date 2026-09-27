"""set_state must not retune the LO when the frequency has not changed.

The frontend sends its whole state -- SignalFreq included -- on every control
change. Retuning unconditionally re-ran SDR_LO_init, opening a fresh ADF4159
context, on each slider drag.
"""

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _headless():
    try:
        import adi  # noqa: F401
    except Exception:
        sys.modules.setdefault("adi", types.ModuleType("adi"))
    try:
        import phaser_headless
    except Exception as exc:  # noqa: BLE001 - zmq/msgpack absent
        pytest.skip(f"phaser_headless unavailable: {exc}")

    h = phaser_headless.PhaserHeadless.__new__(phaser_headless.PhaserHeadless)
    h.SignalFreq = 10.446953e9
    h.retunes = []
    h.set_signal_freq = lambda f: h.retunes.append(float(f))
    return h


def _set_state(h, **state):
    return h.handle_command({"cmd": "set_state", "data": {"state": state}})


def test_unchanged_frequency_does_not_retune():
    h = _headless()
    _set_state(h, SignalFreq=10.446953e9)
    assert h.retunes == []


def test_changed_frequency_retunes_once():
    h = _headless()
    _set_state(h, SignalFreq=10.5e9)
    assert h.retunes == [10.5e9]


def test_frequency_sent_as_string_still_compares():
    h = _headless()
    _set_state(h, SignalFreq="10446953000.0")
    assert h.retunes == []
