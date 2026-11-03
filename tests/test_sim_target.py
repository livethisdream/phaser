"""The simulated HB100's position, which the Polar tab drags.

phaser_sim used to pin the target at boresight, 1 m away. Its angle and range
are now runtime settings, sim_target_angle_deg and sim_target_range_m, so the
beam pattern can be watched following it. These tests pin that the sweep peak
goes where the target is put, that its level falls as 1/r, that the settings
are clamped and survive a re-init, and that they never touch real hardware.
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


def test_defaults_to_boresight_at_one_metre():
    h = _headless()
    assert h.sim_target_angle_deg == 0.0
    assert h.sim_target_range_m == 1.0
    data = h.get_state()["data"]
    assert data["sim_target_angle_deg"] == 0.0
    assert data["sim_target_range_m"] == 1.0


def _quiet_peak_dbfs(patch):
    import phaser_sim

    original = phaser_sim.SimSDR.NOISE_SIGMA
    phaser_sim.SimSDR.NOISE_SIGMA = 0.0
    try:
        return max(_sweep(_headless(patch))["ArrayGain"])
    finally:
        phaser_sim.SimSDR.NOISE_SIGMA = original


@pytest.mark.parametrize("range_m", [0.5, 2.0, 4.0, 10.0])
def test_level_falls_as_one_over_r(range_m):
    """One-way beacon: amplitude 1/r, so 20*log10(r) dB below the 1 m level."""
    ref = _quiet_peak_dbfs({})
    got = _quiet_peak_dbfs({"sim_target_range_m": range_m})
    assert got - ref == pytest.approx(-20 * np.log10(range_m), abs=0.05)


def test_range_does_not_move_the_beam():
    """The wavefront stays planar: range changes the level, not the shape."""
    near = _sweep(_headless({"sim_target_angle_deg": 20.0}))
    far = _sweep(_headless({"sim_target_angle_deg": 20.0, "sim_target_range_m": 5.0}))
    assert np.argmax(near["ArrayGain"]) == np.argmax(far["ArrayGain"])


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

    _set(h, {"sim_target_range_m": 0.01})
    assert h.sim_target_range_m == h.sdr.TARGET_RANGE_MIN_M
    _set(h, {"sim_target_range_m": 1e6})
    assert h.sim_target_range_m == h.sdr.TARGET_RANGE_MAX_M
    assert h.sdr.target_range_m == h.sdr.TARGET_RANGE_MAX_M
    _set(h, {"sim_target_range_m": 3.5})
    assert h.get_state()["data"]["sim_target_range_m"] == 3.5
    _set(h, {"sim_target_range_m": None})
    assert h.sim_target_range_m == 3.5


def test_target_survives_a_hardware_reinit():
    """Find HB100 and Calibrate re-run _do_init_hardware, which builds a new
    SimSDR. The dragged position must come through it."""
    h = _headless({"sim_target_angle_deg": 30.0, "sim_target_range_m": 4.0})
    with contextlib.redirect_stdout(io.StringIO()):
        h._do_init_hardware()
    assert h.sim_target_angle_deg == 30.0
    assert h.sdr.target_angle_deg == 30.0
    assert h.sim_target_range_m == 4.0
    assert h.sdr.target_range_m == 4.0


def test_ignored_off_the_simulator():
    h = _headless()
    h.sim_mode = False
    _set(h, {"sim_target_angle_deg": 30.0, "sim_target_range_m": 4.0})
    assert h.sim_target_angle_deg == 0.0
    assert h.sdr.target_angle_deg == 0.0
    assert h.sim_target_range_m == 1.0
    assert h.sdr.target_range_m == 1.0


def test_hardware_shaped_instance_never_needs_the_setting():
    """On the kit _do_init_hardware takes the real branch, which never sets
    sim_target_angle_deg. The browser still sends the key (as null, echoed
    from get_state) with every set_state, and get_state must still answer.
    Neither may touch the attribute or the SDR."""
    h = _headless()
    h.sim_mode = False
    del h.sim_target_angle_deg
    del h.sim_target_range_m
    sdr_angle = h.sdr.target_angle_deg
    sdr_range = h.sdr.target_range_m
    for angle, range_m in ((None, None), (30.0, 4.0)):
        _set(h, {"sim_target_angle_deg": angle, "sim_target_range_m": range_m,
                 "Rx_gain": 20})
    assert not hasattr(h, "sim_target_angle_deg")
    assert not hasattr(h, "sim_target_range_m")
    assert h.sdr.target_angle_deg == sdr_angle
    assert h.sdr.target_range_m == sdr_range
    data = h.get_state()["data"]
    assert data["sim_target_angle_deg"] is None
    assert data["sim_target_range_m"] is None
