"""The headless backend's CW radar wiring, driven against the simulator.

Covers the seam the unit tests either side of it cannot: phaser_headless
deciding what to hand phaser_cw_radar. Three things used to go wrong here and
each has a test below.

  - start_cw_radar refused outright under --sim, so the radar UI could only be
    exercised with a Phaser on the bench.
  - the taper the frontend pushed was merged into self.cw_params and never
    written to the ADAR1000.
  - vel_max was accepted into cw_params and then ignored, because
    do_cw_radar_frame did not pass it to the DSP.

Built the same way test_beam_pattern.py builds its backend: __new__ so
__init__'s ZMQ binds are skipped, then _do_init_hardware() for the sim stubs.
"""

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

np = pytest.importorskip("numpy")


def _headless():
    """A sim-mode PhaserHeadless with no sockets bound."""
    try:
        import adi  # noqa: F401
    except Exception:
        # pyadi-iio binds libiio at import; sim mode never reaches it.
        sys.modules.setdefault("adi", types.ModuleType("adi"))
    try:
        import phaser_headless
    except Exception as exc:  # noqa: BLE001 - zmq/msgpack absent
        pytest.skip(f"phaser_headless unavailable: {exc}")

    h = phaser_headless.PhaserHeadless.__new__(phaser_headless.PhaserHeadless)
    h.sim_mode = True
    h.c = 299792458
    h._do_init_hardware()

    # Normally set by __init__; the CW handlers need them.
    import threading
    h.mode = "idle"
    h.sweeping = False
    h.cw_params = {}
    h.cw_saved_sdr = {}
    h.cw_lock = threading.Lock()
    return h


def _start(h, **params):
    # A small FFT keeps the test quick; the deployed default is 64k.
    params.setdefault("fft_size", 8192)
    resp = h.start_cw_radar(params)
    assert resp["status"] == "ok", resp
    return resp


# --- sim mode is no longer refused -------------------------------------

def test_cw_radar_starts_in_sim_mode():
    """This is the whole point of the change: no hardware required."""
    h = _headless()
    resp = _start(h)
    assert resp["mode"] == "cw_radar"
    assert h.mode == "cw_radar"
    assert h.sweeping is False, "CW mode must not look like a running sweep"


def test_sim_frame_recovers_the_simulated_target():
    h = _headless()
    _start(h)
    frame = h.do_cw_radar_frame()
    assert np.all(np.isfinite(np.array(frame["spectrum_db"])))
    # The default sim scene's fastest closing target is at +2.5 m/s; the
    # leakage line at 0 is stronger, so just assert the target is present.
    vel = np.array(frame["velocity_axis"])
    spec = np.array(frame["spectrum_db"])
    near = np.abs(vel - 2.5) < 0.6
    assert near.any()
    assert spec[near].max() > np.median(spec) + 10


def test_stopping_returns_to_idle_and_restores_the_scene():
    h = _headless()
    _start(h)
    assert h.stop_cw_radar()["mode"] == "idle"
    assert h.mode == "idle"
    # The simulator is back on the beamforming scene, so a sweep still works.
    assert h.sdr._cw_enable is False


# --- the taper actually reaches the array ------------------------------

def _latched(h):
    return [h.array.elements[i + 1].latched_gain for i in range(8)]


def test_start_latches_the_requested_taper():
    """readParams() now sends a taper on Start; it has to reach the ADAR1000."""
    from phaser_radar_dsp import TAPER_PRESETS

    h = _headless()
    _start(h, taper="blackman")
    assert _latched(h) == TAPER_PRESETS["blackman"]


def test_changing_the_taper_while_running_is_live():
    """The Rect/Hann/Black buttons push through set_cw_radar_params."""
    from phaser_radar_dsp import TAPER_PRESETS

    h = _headless()
    _start(h, taper="blackman")
    resp = h.set_cw_radar_params({"taper": "rect"})
    assert resp["status"] == "ok", resp
    assert _latched(h) == TAPER_PRESETS["rect"]


def test_leaving_cw_mode_restores_the_beamforming_taper():
    """Otherwise the next sweep runs under the radar's Blackman illumination.

    Restored through _apply_gain_cal, because gainList is on the frontend's
    0-100 scale while the ADAR1000 register is 0-127: writing it raw would put
    the array back at 79% of full scale with the gain calibration dropped.
    """
    h = _headless()
    expected = h._apply_gain_cal(h.gainList)
    _start(h, taper="blackman")
    h.stop_cw_radar()
    assert _latched(h) == expected
    # And that is genuinely the full-scale value, not the raw 0-100 one.
    assert max(expected) > 100


def test_a_failed_entry_unwinds_the_array_and_leaves_idle():
    """enter_cw_mode moves the LO and taper before it touches the SDR.

    A failure partway through used to leave the array illuminated for radar
    with the LO parked on the CW mixing frequency, and the next sweep then saw
    nothing with no error to explain it.
    """
    h = _headless()
    expected = h._apply_gain_cal(h.gainList)

    # Fail at the first SDR write, which is after the taper has been latched.
    class Boom(Exception):
        pass

    real_sdr = h.sdr

    class ExplodingSDR:
        def __getattr__(self, name):
            return getattr(real_sdr, name)

        def __setattr__(self, name, value):
            raise Boom("SDR write failed")

    h.sdr = ExplodingSDR()
    resp = h.start_cw_radar({"taper": "blackman", "fft_size": 8192})

    assert resp["status"] == "error"
    assert h.mode == "idle"
    assert h.sweeping is False
    assert _latched(h) == expected, "failed entry left the radar taper on the array"


# --- vel_max reaches the DSP -------------------------------------------

@pytest.mark.parametrize("vel_max", [8.0, 40.0])
def test_vel_max_reaches_the_dsp(vel_max):
    h = _headless()
    _start(h, vel_max=vel_max)
    vel = np.array(h.do_cw_radar_frame()["velocity_axis"])
    assert np.all(np.abs(vel) <= vel_max + 1e-6)


def test_a_wider_vel_max_returns_more_bins():
    h = _headless()
    _start(h, vel_max=8.0)
    narrow = len(h.do_cw_radar_frame()["velocity_axis"])
    h.set_cw_radar_params({"vel_max": 40.0})
    wide = len(h.do_cw_radar_frame()["velocity_axis"])
    assert wide > narrow, "Velocity Max slider still does not reach the crop"


# --- state reporting ----------------------------------------------------

def test_state_reports_defaults_merged_with_overrides():
    h = _headless()
    _start(h, taper="rect", vel_max=12.0)
    data = h.get_cw_radar_state()["data"]
    assert data["running"] is True
    assert data["params"]["taper"] == "rect"
    assert data["params"]["vel_max"] == 12.0
    # Untouched keys still come from the defaults table.
    assert data["params"]["signal_freq"] == 100_000
