"""A finished calibration must stay reportable, not vanish after one poll."""

import sys
import types

import pytest


@pytest.fixture(scope="module")
def PhaserHeadless():
    """Import phaser_headless with the hardware libraries stubbed.

    Only `adi` needs faking: it is the one import that requires libiio and real
    hardware. Everything else phaser_headless imports at module level is either
    stdlib or already installed.
    """
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
    return phaser_headless.PhaserHeadless


@pytest.fixture
def app(PhaserHeadless):
    """A PhaserHeadless with only the calibration fields set.

    __new__ rather than __init__ on purpose: __init__ talks to the radio, and
    the calibration state machine is independent of it.
    """
    obj = object.__new__(PhaserHeadless)
    obj.cal_process = None
    obj.cal_task = None
    obj.cal_log = []
    obj.cal_last_result = None
    return obj


class FakeProc:
    """A subprocess that has already exited."""
    def __init__(self, returncode):
        self.returncode = returncode
    def poll(self):
        return self.returncode


def test_failure_is_reported_on_every_poll(app):
    """The bug: the outcome was returned once and then discarded, so a client
    reconnecting during that single 2s window never learned the run failed and
    the modal just sat there looking frozen."""
    app.cal_process = FakeProc(1)
    app.cal_task = "find_hb100"
    app.cal_log = ["ImportError: cannot import name 'spec_est'"]

    first = app.get_calibration_status()
    assert first["running"] is False
    assert first["returncode"] == 1
    assert first["success"] is False

    for _ in range(3):
        again = app.get_calibration_status()
        assert again["returncode"] == 1, "outcome forgotten after the first poll"
        assert again["success"] is False
        assert "spec_est" in "\n".join(again["last_lines"])


def test_idle_before_any_run(app):
    status = app.get_calibration_status()
    assert status["running"] is False
    assert status["task"] is None
    assert "returncode" not in status


def test_cancelling_an_already_dead_process_reports_why(app):
    """A crash looks exactly like this from the caller's side. Reporting 'no
    calibration running' threw away the only evidence of what went wrong."""
    app.cal_process = FakeProc(1)
    app.cal_task = "find_hb100"
    app.cal_log = ["Traceback (most recent call last):", "ImportError: ..."]

    result = app.cancel_calibration()
    assert result.get("returncode") == 1
    assert result.get("success") is False


def test_success_is_also_retained(app, monkeypatch):
    app.cal_process = FakeProc(0)
    app.cal_task = "find_hb100"
    app.cal_log = ["Calibration saved successfully!"]
    monkeypatch.setattr(app, "_reload_calibration", lambda task: None, raising=False)

    assert app.get_calibration_status()["success"] is True
    assert app.get_calibration_status()["success"] is True
