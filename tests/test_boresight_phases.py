"""Radar modes and startup latch boresight phases instead of inheriting them.

Only the beamforming sweep ever wrote the ADAR1000 Rx phases. A radar mode
started before any sweep had run -- the first thing anyone does after a
service restart -- captured through phase registers nothing had written, and
on phaser-01 the array was deaf: the Tx->Rx leakage at 0 m was the only
return, and a swinging pot at 1.8 m never showed. Writing boresight (the phase
cal alone) and latching it made the pot appear at 1.68 m on the next frame
(docs/calibration-bugs.md, bug 5).

The stub elements model the shadow/latched split, so every assertion here is
on `latched_phase`: a write that is never latched does not count, on the kit
or in the sim.
"""

import sys
import threading
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

np = pytest.importorskip("numpy")

try:
    import adi  # noqa: F401
except Exception:
    # pyadi-iio binds libiio at import; nothing here reaches it.
    sys.modules.setdefault("adi", types.ModuleType("adi"))

import phaser_cw_radar  # noqa: E402
import phaser_fmcw_radar as fm  # noqa: E402
import phaser_radar_dsp as dsp  # noqa: E402
from phaser_sim import make_stub_array  # noqa: E402

# Non-trivial and partly negative, like a real kit's (phaser-01's runs from
# 0 to -54.9 and back up to 74.1), so a dropped cal or a missing modulo shows.
PCAL = [0.0, -16.9, -41.3, -54.9, 74.1, 48.0, 45.7, 40.3]
BORESIGHT = [p % 360 for p in PCAL]
SENTINEL = 123.0


def _latched_phases(array):
    return [array.elements[i + 1].latched_phase for i in range(8)]


def _scramble(array):
    """Put every element somewhere boresight is not, shadow and latched."""
    for el in array.elements.values():
        el.rx_phase = SENTINEL
        el.latched_phase = SENTINEL


# --- the helper ----------------------------------------------------------

def test_apply_boresight_latches_the_phase_cal():
    array = make_stub_array()
    _scramble(array)
    before = array.latch_count
    assert phaser_cw_radar.apply_boresight(array, PCAL) == PCAL
    assert _latched_phases(array) == pytest.approx(BORESIGHT)
    assert array.latch_count == before + 1


def test_apply_boresight_without_a_cal_latches_zeros():
    array = make_stub_array()
    _scramble(array)
    assert phaser_cw_radar.apply_boresight(array, None) == [0.0] * 8
    assert _latched_phases(array) == [0.0] * 8


def test_apply_boresight_pads_a_short_cal():
    array = make_stub_array()
    _scramble(array)
    phaser_cw_radar.apply_boresight(array, [10.0, 20.0])
    assert _latched_phases(array) == [10.0, 20.0] + [0.0] * 6


def test_apply_boresight_with_no_array_is_a_no_op():
    assert phaser_cw_radar.apply_boresight(None, PCAL) is None


def test_apply_boresight_raises_rather_than_leaving_the_array_deaf():
    """apply_taper swallows a failed write; this must not, or the mode runs
    deaf with nothing to say why -- the failure this exists to prevent."""
    class Broken:
        elements = {i + 1: None for i in range(8)}

        def latch_rx_settings(self):
            raise AssertionError("never reached")

    with pytest.raises(AttributeError):
        phaser_cw_radar.apply_boresight(Broken(), PCAL)


# --- FMCW hardware sequence ----------------------------------------------

def test_enter_fmcw_latches_boresight_with_the_cal():
    from phaser_sim import SimSDR, make_stub_fmcw_hardware

    array = make_stub_array()
    _scramble(array)
    sdr = SimSDR(array, signal_freq=10.0e9, element_spacing=0.014)
    hw = make_stub_fmcw_hardware(types.SimpleNamespace(gpio_burst=0))
    cfg = dsp.fmcw_config({"center_freq": 2.0e9}, preset="range_sync")
    saved = {}
    try:
        fm.enter_fmcw_mode(sdr, cfg, saved, hw, array=array, phase_cal=PCAL)
        assert _latched_phases(array) == pytest.approx(BORESIGHT)
    finally:
        fm.exit_fmcw_mode(sdr, saved, hw, array=array)


# --- the backend ----------------------------------------------------------

def _headless(monkeypatch):
    """A sim-mode PhaserHeadless with no sockets bound and PCAL as its cal."""
    try:
        import phaser_headless
    except Exception as exc:  # noqa: BLE001 - zmq/msgpack absent
        pytest.skip(f"phaser_headless unavailable: {exc}")
    monkeypatch.setattr(phaser_headless, "load_phase_cal", lambda *a, **k: list(PCAL))

    h = phaser_headless.PhaserHeadless.__new__(phaser_headless.PhaserHeadless)
    h.sim_mode = True
    h.c = 299792458
    h._do_init_hardware()

    h.mode = "idle"
    h.sweeping = False
    h.cw_params = {}
    h.cw_saved_sdr = {}
    h.cw_lock = threading.Lock()
    h.fmcw_params = {}
    h.fmcw_saved = {}
    h.fmcw_hw = None
    h.fmcw_cfg = None
    h.fmcw_plan = None
    h.cal_process = None
    h.frames = []
    h.broadcast_to_ws = h.frames.append
    return h


def test_startup_latches_boresight_before_any_sweep(monkeypatch):
    h = _headless(monkeypatch)
    assert _latched_phases(h.array) == pytest.approx(BORESIGHT)


def test_cw_start_latches_boresight(monkeypatch):
    h = _headless(monkeypatch)
    _scramble(h.array)
    resp = h.start_cw_radar({"fft_size": 8192})
    assert resp["status"] == "ok", resp
    try:
        assert _latched_phases(h.array) == pytest.approx(BORESIGHT)
    finally:
        h.stop_cw_radar()


def test_fmcw_start_latches_boresight(monkeypatch):
    h = _headless(monkeypatch)
    _scramble(h.array)
    resp = h.start_fmcw_radar({})
    assert resp["status"] == "ok", resp
    try:
        assert _latched_phases(h.array) == pytest.approx(BORESIGHT)
    finally:
        h.stop_fmcw_radar()


@pytest.mark.parametrize("mode", ["cw", "fmcw"])
def test_a_failed_boresight_write_fails_the_start_and_unwinds(monkeypatch, mode):
    """Better a refused start than a mode that runs deaf."""
    h = _headless(monkeypatch)
    expected_gains = h._apply_gain_cal(h.gainList)

    def boom(array, phase_cal=None):
        raise IOError("ADAR1000 write failed")

    monkeypatch.setattr(phaser_cw_radar, "apply_boresight", boom)
    if mode == "cw":
        resp = h.start_cw_radar({"taper": "blackman", "fft_size": 8192})
    else:
        resp = h.start_fmcw_radar({"taper": "blackman"})

    assert resp["status"] == "error"
    assert "ADAR1000 write failed" in resp.get("message", "")
    assert h.mode == "idle"
    assert [h.array.elements[i + 1].latched_gain for i in range(8)] == expected_gains


# --- FMCW leftovers are reset at startup (calibration-bugs.md, bug 6) ----------

class _Pins:
    def __init__(self, uri):
        self.uri = uri
        self.gpio_phaser_enable = True


class _Synth:
    def __init__(self, uri):
        self.uri = uri
        self.ramp_mode = "single_sawtooth_burst"
        self.tx_trig_en = 1
        self.enable = 1


def _bare_backend(monkeypatch, tddn, pins=_Pins, synth=_Synth):
    try:
        import phaser_headless
    except Exception as exc:  # pragma: no cover - depends on the runner
        pytest.skip(f"phaser_headless unavailable: {exc}")
    made = {"pins": [], "synth": []}
    monkeypatch.setattr(phaser_headless.adi, "tddn", tddn, raising=False)
    monkeypatch.setattr(phaser_headless.adi, "one_bit_adc_dac",
                        lambda uri: made["pins"].append(pins(uri)) or made["pins"][-1],
                        raising=False)
    monkeypatch.setattr(phaser_headless.adi, "adf4159",
                        lambda uri: made["synth"].append(synth(uri)) or made["synth"][-1],
                        raising=False)
    h = phaser_headless.PhaserHeadless.__new__(phaser_headless.PhaserHeadless)
    h.sim_mode = False
    h.sdr_ip = "ip:192.168.2.1"
    h.rpi_ip = "ip:localhost"
    h._made = made
    return h


def test_startup_releases_a_tdd_left_holding_the_rx_dma(monkeypatch):
    released = []

    class Chan:
        polarity = False

    class Tdd:
        def __init__(self, uri):
            self.uri = uri
            self.enable = False
            self.channel = [Chan(), Chan(), Chan()]

        def __setattr__(self, k, v):
            object.__setattr__(self, k, v)
            if k == "enable":
                released.append(v)

    made = []
    h = _bare_backend(monkeypatch, lambda uri: made.append(Tdd(uri)) or made[-1])
    h._reset_fmcw_leftovers()
    assert made[0].uri == "ip:192.168.2.1"
    assert made[0].channel[1].polarity is True and made[0].channel[2].polarity is True
    assert released[-2:] == [True, False]


def test_startup_clears_phaser_enable_and_stops_the_ramp(monkeypatch):
    def no_tdd(uri):
        raise OSError("No device found")

    h = _bare_backend(monkeypatch, no_tdd)
    h._reset_fmcw_leftovers()
    pins, synth = h._made["pins"][0], h._made["synth"][0]
    assert pins.uri == "ip:192.168.2.1" and pins.gpio_phaser_enable is False
    assert synth.uri == "ip:localhost"
    assert (synth.ramp_mode, synth.tx_trig_en, synth.enable) == ("disabled", 0, 0)


def test_startup_survives_a_kit_without_pins_or_synth(monkeypatch):
    def gone(uri):
        raise OSError("No device found")

    h = _bare_backend(monkeypatch, gone, pins=gone, synth=gone)
    h._reset_fmcw_leftovers()  # must not raise


def test_startup_survives_a_pluto_without_a_tdd(monkeypatch):
    def no_tdd(uri):
        raise OSError("No device found")

    h = _bare_backend(monkeypatch, no_tdd)
    h._reset_fmcw_leftovers()  # must not raise


def test_startup_release_is_skipped_in_sim(monkeypatch):
    def boom(uri):
        raise AssertionError("sim must not open a TDD")

    h = _bare_backend(monkeypatch, boom, pins=boom, synth=boom)
    h.sim_mode = True
    h._reset_fmcw_leftovers()
