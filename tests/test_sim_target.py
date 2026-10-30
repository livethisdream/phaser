"""The simulated HB100's position, which the Scene tab drags.

phaser_sim used to pin the target at boresight. It is now a runtime setting,
sim_target_angle_deg, so the beam pattern can be watched following it. These
tests pin that the sweep peak goes where the target is put, that the setting
is clamped and survives a re-init, and that it never touches real hardware.
"""

import contextlib
import io
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

np = pytest.importorskip("numpy")


def _headless(patch=None):
    sys.modules.setdefault("adi", types.ModuleType("adi"))
    try:
        import phaser_headless
    except Exception as exc:  # noqa: BLE001 - zmq/msgpack absent
        pytest.skip(f"phaser_headless unavailable: {exc}")

    with contextlib.redirect_stdout(io.StringIO()):
        h = phaser_headless.PhaserHeadless.__new__(phaser_headless.PhaserHeadless)
        h.sim_mode = True
        h.c = 299792458
        h._do_init_hardware()
        if patch:
            h.handle_command({"cmd": "set_state", "data": {"state": patch}})
    # Normally set by __init__, which this skips; get_state reads them.
    h.sweeping = False
    h._shutdown_permitted = False
    return h


def _set(h, patch):
    with contextlib.redirect_stdout(io.StringIO()):
        h.handle_command({"cmd": "set_state", "data": {"state": patch}})


def _sweep(h):
    with contextlib.redirect_stdout(io.StringIO()):
        return h.do_sweep()


def test_defaults_to_boresight():
    h = _headless()
    assert h.sim_target_angle_deg == 0.0
    assert h.get_state()["data"]["sim_target_angle_deg"] == 0.0


@pytest.mark.parametrize("angle", [-40.0, -12.0, 20.0, 45.0])
def test_beam_peak_follows_the_target(angle):
    h = _headless({"sim_target_angle_deg": angle, "ignore_res": False, "steer_res": 1.0})
    with contextlib.redirect_stdout(io.StringIO()):
        h.handle_command({"cmd": "set_sweep_params",
                          "data": {"steer_min": -80, "steer_max": 80}})
    out = _sweep(h)
    angles = np.asarray(out["ArrayAngle"])
    gain = np.asarray(out["ArrayGain"])
    assert abs(angles[int(np.argmax(gain))] - angle) <= 2.0


def test_set_state_clamps_and_get_state_echoes():
    h = _headless({"sim_target_angle_deg": 120.0})
    assert h.sim_target_angle_deg == 90.0
    assert h.sdr.target_angle_deg == 90.0
    _set(h, {"sim_target_angle_deg": -33.5})
    assert h.get_state()["data"]["sim_target_angle_deg"] == -33.5
    # The frontend echoes get_state back; a null must not crash or move it.
    _set(h, {"sim_target_angle_deg": None})
    assert h.sim_target_angle_deg == -33.5


def test_target_survives_a_hardware_reinit():
    """Find HB100 and Calibrate re-run _do_init_hardware, which builds a new
    SimSDR. The dragged position must come through it."""
    h = _headless({"sim_target_angle_deg": 30.0})
    with contextlib.redirect_stdout(io.StringIO()):
        h._do_init_hardware()
    assert h.sim_target_angle_deg == 30.0
    assert h.sdr.target_angle_deg == 30.0


def test_ignored_off_the_simulator():
    h = _headless()
    h.sim_mode = False
    _set(h, {"sim_target_angle_deg": 30.0})
    assert h.sim_target_angle_deg == 0.0
    assert h.sdr.target_angle_deg == 0.0
