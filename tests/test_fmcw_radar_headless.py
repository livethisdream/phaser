"""The fmcw_radar mode on a sim-mode PhaserHeadless (Phase C2).

--sim runs the same enter_fmcw_mode / exit_fmcw_mode a kit runs, against stub
ADF4159/TDD/GPIO handles, and SimSDR lays the chirps out by the plan the way
the TDD delivers them. So these exercise the whole mode path -- dispatcher,
hardware sequence, slicing, DSP -- with no kit attached.
"""

import sys
import threading
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

np = pytest.importorskip("numpy")

import phaser_radar_dsp as dsp  # noqa: E402

SDR_KEYS = ("sample_rate", "rx_buffer_size", "rx_lo", "tx_lo",
            "rx_hardwaregain_chan0", "rx_hardwaregain_chan1",
            "tx_hardwaregain_chan0", "tx_hardwaregain_chan1", "tx_cyclic_buffer")


def _headless():
    """A sim-mode PhaserHeadless with no sockets bound."""
    try:
        import adi  # noqa: F401
    except Exception:
        sys.modules.setdefault("adi", types.ModuleType("adi"))
    try:
        import phaser_headless
    except Exception as exc:  # noqa: BLE001 - zmq/msgpack absent
        pytest.skip(f"phaser_headless unavailable: {exc}")

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


def _sdr_state(h):
    return {k: getattr(h.sdr, k) for k in SDR_KEYS}


def _latched(h):
    return [h.array.elements[i + 1].latched_gain for i in range(8)]


def _start(h, **params):
    resp = h.start_fmcw_radar(params)
    assert resp["status"] == "ok", resp
    return resp


# --- entering and ranging ----------------------------------------------

def test_fmcw_starts_in_sim_with_the_synced_ramp():
    h = _headless()
    assert _start(h)["mode"] == "fmcw_radar"
    assert h.sweeping is False
    assert h.fmcw_plan["ramp_mode"] == dsp.RAMP_SYNCED
    assert h.fmcw_preset == dsp.FMCW_DEFAULT_PRESET
    assert h.sdr._fmcw_enable is True


@pytest.mark.parametrize("preset", ["range", "range_sync", "cfar"])
def test_single_chirp_presets_range_the_nearest_target(preset):
    """The sim's strongest return is the stationary corner reflector at 1 m,
    and it comes back there through the real slicing."""
    h = _headless()
    _start(h, preset=preset)
    frame = h.do_fmcw_radar_frame()
    assert frame["kind"] == "range_profile"
    assert frame["peak_range"] == pytest.approx(1.0, abs=0.45)


def test_range_doppler_preset_finds_range_and_velocity():
    """With 2-pulse MTI the stationary reflector cancels and the mover walking
    in at 3.5 m, 1.8 m/s is the peak."""
    h = _headless()
    _start(h, preset="range_doppler", mti="2pulse")
    frame = h.do_fmcw_radar_frame()
    assert frame["kind"] == "range_doppler"
    assert frame["num_chirps"] == 256
    assert frame["peak_range"] == pytest.approx(3.5, abs=0.5)
    assert abs(frame["peak_velocity"]) == pytest.approx(1.8, abs=0.3)


def test_frames_report_their_timing_and_serialize():
    import json
    h = _headless()
    _start(h, preset="range_sync", cfar=True)
    frame = h.do_fmcw_radar_frame()
    assert frame["capture_ms"] >= 0 and frame["process_ms"] >= 0
    assert frame["detections"] >= 1
    json.dumps(frame)


def test_the_main_loop_step_broadcasts_an_fmcw_frame():
    h = _headless()
    _start(h, preset="range_sync")
    h._radar_frame_step()
    assert h.frames[-1]["type"] == "fmcw_radar_frame"


# --- center_freq --------------------------------------------------------

def test_center_freq_defaults_to_the_kits_characterized_if():
    h = _headless()
    _start(h)
    assert h.fmcw_cfg["center_freq"] == h.Rx_freq
    assert h.sdr.rx_lo == int(h.Rx_freq)


def test_an_explicit_center_freq_wins():
    h = _headless()
    _start(h, center_freq=2.05e9)
    assert h.sdr.rx_lo == int(2.05e9)


# --- leaving ------------------------------------------------------------

def test_stop_puts_the_sdr_taper_and_lo_back():
    h = _headless()
    before = _sdr_state(h)
    taper = h._apply_gain_cal(h.gainList)
    _start(h, preset="range_doppler")
    synth, tdd = h.fmcw_hw.synth, h.fmcw_hw.tdd
    assert h.stop_fmcw_radar()["mode"] == "idle"
    assert _sdr_state(h) == before
    assert _latched(h) == taper
    assert synth.ramp_mode == "disabled"
    assert synth.frequency == int(h.LO_freq / 4)
    assert tdd.enable is False
    assert h.sdr._fmcw_enable is False
    assert h.fmcw_hw is None and h.fmcw_plan is None


def test_the_beam_is_where_it_was_after_fmcw():
    """Bench step C5.7, in sim: the HB100 beam peaks at the same angle after
    an FMCW session as before it."""
    h = _headless()
    h._set_mode("sweep")
    before = h.do_sweep()["peak_angle_deg"]
    _start(h, preset="range_doppler")
    h.stop_fmcw_radar()
    assert h._set_mode("sweep")["mode"] == "sweep"
    assert h.do_sweep()["peak_angle_deg"] == pytest.approx(before, abs=1.0)


def test_cw_fmcw_cw_idle_restores_at_every_step():
    h = _headless()
    before = _sdr_state(h)
    assert h.start_cw_radar({"fft_size": 8192})["status"] == "ok"
    _start(h, preset="range_sync")
    assert h.mode == "fmcw_radar" and h.sdr._cw_enable is False
    assert h.start_cw_radar({"fft_size": 8192})["status"] == "ok"
    assert h.mode == "cw_radar" and h.sdr._fmcw_enable is False
    h.stop_cw_radar()
    assert h.mode == "idle"
    assert _sdr_state(h) == before


# --- refusals and failures ---------------------------------------------

def test_an_unavailable_kit_refuses_with_the_probes_reason():
    h = _headless()
    h.fmcw_capability = {"available": False, "ramp": None, "source": "hardware",
                         "reason": "Pluto firmware v0.38 is older than v0.39",
                         "checks": []}
    resp = h.start_fmcw_radar({})
    assert resp["status"] == "error"
    assert "v0.38" in resp["message"]
    assert h.mode == "idle"


def _free_running(h):
    h.fmcw_capability = {"available": True, "ramp": dsp.RAMP_FREE_RUNNING,
                         "source": "hardware",
                         "reason": "Pluto GPIOs missing: phaser_enable", "checks": []}


def test_without_chirp_sync_range_doppler_is_refused():
    h = _headless()
    _free_running(h)
    resp = h.start_fmcw_radar({"preset": "range_doppler"})
    assert resp["status"] == "error"
    assert "chirp sync" in resp["message"]
    assert h.mode == "idle"


def test_without_chirp_sync_the_range_lab_still_runs():
    h = _headless()
    _free_running(h)
    _start(h, preset="range")
    assert h.fmcw_plan["synced"] is False
    frame = h.do_fmcw_radar_frame()
    assert frame["kind"] == "range_profile"
    assert frame["peak_range"] == pytest.approx(1.0, abs=0.45)


def test_a_failed_enter_unwinds_and_lands_idle(monkeypatch):
    import phaser_sim

    real = phaser_sim.make_stub_fmcw_hardware

    class _BrokenSynth(phaser_sim._StubGPIOs):
        def __setattr__(self, name, value):
            if name == "enable":
                raise IOError("ADF4159 write failed")
            super().__setattr__(name, value)

    def broken(gpios):
        hw = real(gpios)
        hw.synth = _BrokenSynth()
        return hw

    monkeypatch.setattr(phaser_sim, "make_stub_fmcw_hardware", broken)
    h = _headless()
    before = _sdr_state(h)
    taper = h._apply_gain_cal(h.gainList)
    resp = h.start_fmcw_radar({"preset": "range_doppler"})
    assert resp["status"] == "error" and "ADF4159" in resp["message"]
    assert h.mode == "idle" and h.sweeping is False
    assert _sdr_state(h) == before
    assert _latched(h) == taper
    assert h.fmcw_hw is None and h.sdr._fmcw_enable is False


# --- params -------------------------------------------------------------

def test_a_processing_param_applies_without_reentering():
    h = _headless()
    _start(h, preset="range_doppler")
    plan = h.fmcw_plan
    resp = h.set_fmcw_radar_params({"mti": "3pulse", "range_max": 10})
    assert resp["reentered"] is False
    assert h.fmcw_plan is plan
    assert h.fmcw_cfg["mti"] == "3pulse" and h.fmcw_cfg["range_max"] == 10


def test_a_ramp_param_reenters_with_a_new_plan():
    h = _headless()
    _start(h, preset="range_doppler")
    resp = h.set_fmcw_radar_params({"num_chirps": 64})
    assert resp["status"] == "ok" and resp["reentered"] is True
    assert h.mode == "fmcw_radar"
    assert h.fmcw_plan["num_chirps"] == 64


def test_the_taper_is_live():
    h = _headless()
    _start(h, taper="blackman")
    h.set_fmcw_radar_params({"taper": "rect"})
    assert _latched(h) == dsp.TAPER_PRESETS["rect"]


def test_params_set_while_idle_are_kept_for_the_next_start():
    h = _headless()
    resp = h.set_fmcw_radar_params({"preset": "cfar"})
    assert resp["reentered"] is False and h.mode == "idle"
    _start(h)
    assert h.fmcw_preset == "cfar"


def test_state_reports_cfg_plan_presets_and_capability():
    h = _headless()
    idle = h.get_fmcw_radar_state()["data"]
    assert idle["running"] is False and idle["plan"] is None
    assert idle["cfg"]["center_freq"] == h.Rx_freq
    assert set(idle["presets"]) == set(dsp.FMCW_PRESETS)
    _start(h, preset="range_sync")
    data = h.get_fmcw_radar_state()["data"]
    assert data["running"] is True and data["preset"] == "range_sync"
    assert data["plan"]["num_chirps"] == 1
    assert data["capability"]["available"] is True


def test_state_reports_a_bad_preset_instead_of_raising():
    h = _headless()
    h.set_fmcw_radar_params({"preset": "nope"})
    data = h.get_fmcw_radar_state()["data"]
    assert "unknown FMCW preset" in data["error"]


# --- other ways out of the mode ----------------------------------------

def test_calibration_tears_fmcw_down_first(monkeypatch):
    import phaser_headless

    seen = {}

    def fake_popen(*args, **kwargs):
        seen["mode"] = h.mode
        seen["fmcw_hw"] = h.fmcw_hw
        raise OSError("not starting a real calibration in a test")

    monkeypatch.setattr(phaser_headless.subprocess, "Popen", fake_popen)
    h = _headless()
    _start(h, preset="range_sync")
    h.run_calibration("find_hb100")
    assert seen == {"mode": "idle", "fmcw_hw": None}


def test_shutdown_leaves_fmcw(monkeypatch):
    h = _headless()
    closed = []
    h.pub_socket = types.SimpleNamespace(close=lambda: closed.append("pub"))
    h.rep_socket = types.SimpleNamespace(close=lambda: closed.append("rep"))
    h.ctx = types.SimpleNamespace(term=lambda: closed.append("ctx"))
    _start(h, preset="range_sync")
    synth, tdd = h.fmcw_hw.synth, h.fmcw_hw.tdd
    h.stop()
    assert h.mode == "idle"
    assert tdd.enable is False and synth.ramp_mode == "disabled"


def test_a_transition_waits_for_the_frame_in_progress(monkeypatch):
    """A stop from the WebSocket thread must not tear the hardware down while
    the main loop is mid-capture."""
    import phaser_fmcw_radar

    h = _headless()
    _start(h, preset="range_sync")
    real = phaser_fmcw_radar.capture_fmcw_frame
    started, release = threading.Event(), threading.Event()

    def slow_capture(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return real(*args, **kwargs)

    monkeypatch.setattr(phaser_fmcw_radar, "capture_fmcw_frame", slow_capture)
    frame_thread = threading.Thread(target=h._radar_frame_step)
    frame_thread.start()
    assert started.wait(5)

    stop_thread = threading.Thread(target=h.stop_fmcw_radar)
    stop_thread.start()
    stop_thread.join(0.3)
    assert stop_thread.is_alive(), "stop did not wait for the frame"
    assert h.mode == "fmcw_radar" and h.fmcw_hw is not None

    release.set()
    frame_thread.join(5)
    stop_thread.join(5)
    assert h.mode == "idle"
    assert h.frames[-1]["type"] == "fmcw_radar_frame"


# --- frame payload (C3) -------------------------------------------------

import json  # noqa: E402
import time  # noqa: E402

# Per-frame JSON budgets. range_doppler at ~5 fps is the one that matters:
# 32 KB there is ~160 KB/s to each browser.
PAYLOAD_BUDGET_KB = {"range_doppler": 32, "range_doppler_far": 128,
                     "range": 8, "range_sync": 8, "cfar": 8}


@pytest.mark.parametrize("preset", sorted(PAYLOAD_BUDGET_KB))
def test_every_preset_frame_is_json_and_within_budget(preset):
    h = _headless()
    _start(h, preset=preset, cfar=True)
    frame = {"type": "fmcw_radar_frame", "timestamp": 0.0, "data": h.do_fmcw_radar_frame()}
    text = json.dumps(frame)  # raises on any numpy scalar left in the payload
    assert len(text) / 1024 <= PAYLOAD_BUDGET_KB[preset], f"{len(text) / 1024:.1f} KB"


def test_range_doppler_frame_carries_a_decodable_map_on_its_axes():
    h = _headless()
    _start(h, preset="range_doppler")
    f = h.do_fmcw_radar_frame()
    m = dsp.decode_db_u8(f["map"])
    assert m.shape == (len(f["range_axis"]), len(f["velocity_axis"]))
    # The reported peak is the brightest decoded cell outside the zero-Doppler
    # exclusion (peak_exclude), not the brightest overall.
    vel = np.array(f["velocity_axis"])
    moving = np.where(np.abs(vel)[None, :] >= dsp.FMCW_PROC_DEFAULTS["peak_exclude"], m, -np.inf)
    r, v = np.unravel_index(int(np.argmax(moving)), m.shape)
    assert f["range_axis"][r] == pytest.approx(f["peak_range"], abs=1e-3)
    assert f["velocity_axis"][v] == pytest.approx(f["peak_velocity"], abs=1e-3)


def test_range_profile_frame_carries_its_trace_and_cfar():
    h = _headless()
    _start(h, preset="range_sync", cfar=True)
    f = h.do_fmcw_radar_frame()
    n = len(f["range_axis"])
    assert len(f["profile_db"]) == n == len(f["cfar_threshold_db"])
    assert f["detections"] == len(f["detection_indices"])
    assert all(0 <= i < n for i in f["detection_indices"])
    above = [f["profile_db"][i] > f["cfar_threshold_db"][i] for i in f["detection_indices"]]
    assert all(above)


def test_cfar_arrays_are_absent_when_cfar_is_off():
    h = _headless()
    _start(h, preset="range_sync")
    f = h.do_fmcw_radar_frame()
    assert "cfar_threshold_db" not in f and "detection_indices" not in f


def test_null_center_freq_means_this_kits_if():
    """The radar UI sends center_freq: null for an empty field, as for CW."""
    h = _headless()
    h.fmcw_params = {"preset": "range_doppler", "center_freq": 2.0e9}
    _start(h, center_freq=None)
    assert h.fmcw_cfg["center_freq"] == h.Rx_freq


def test_null_range_max_means_the_presets_own():
    """An empty Range Max field: the preset's range, not an uncropped one --
    at start, and as a live update."""
    h = _headless()
    _start(h, preset="range_doppler_far", range_max=None)
    assert h.fmcw_cfg["range_max"] == 100.0
    h.set_fmcw_radar_params({"range_max": 30.0})
    assert h.fmcw_cfg["range_max"] == 30.0
    h.set_fmcw_radar_params({"range_max": None})
    assert h.fmcw_cfg["range_max"] == 100.0


def test_start_while_running_applies_the_new_params():
    """A Start from a reloaded page used to be a silent no-op on the config."""
    h = _headless()
    _start(h, preset="range_doppler")
    assert h.fmcw_preset == "range_doppler"
    resp = h.start_fmcw_radar({"preset": "range_sync"})
    assert resp["status"] == "ok"
    assert h.fmcw_preset == "range_sync" and h.mode == "fmcw_radar"
    h.start_fmcw_radar({"mti": "2pulse"})
    assert h.fmcw_cfg["mti"] == "2pulse"


def test_radar_frames_are_paced_to_the_cap(monkeypatch):
    """range_sync ran at ~475 fps in --sim and drowned the browser."""
    h = _headless()
    clock = {"t": 0.0}
    slept = []
    monkeypatch.setattr(time, "perf_counter", lambda: clock["t"])
    monkeypatch.setattr(time, "sleep", lambda s: (slept.append(s), clock.__setitem__("t", clock["t"] + s)))
    h._pace_radar_frames()            # first frame: nothing to wait for
    clock["t"] += 0.002               # a 2 ms frame
    h._pace_radar_frames()
    assert slept == [pytest.approx(1 / h.RADAR_MAX_FPS - 0.002)]
    clock["t"] += 0.5                 # a slow frame: no sleep
    h._pace_radar_frames()
    assert len(slept) == 1


def test_range_doppler_peak_follows_the_moving_target():
    """phaser-01, 2026-10-30: Peak Range / Velocity sat at 0 m, 0 m/s -- the
    leakage, put at 0 m by the calibration -- while the map showed a hand.
    The simulator's strongest return is its stationary 1 m target."""
    h = _headless()
    _start(h, preset="range_doppler")
    f = h.do_fmcw_radar_frame()
    assert abs(f["peak_velocity"]) >= 0.3
    h.set_fmcw_radar_params({"peak_exclude": 0.0})   # live; 0 = the old search
    f0 = h.do_fmcw_radar_frame()
    assert abs(f0["peak_velocity"]) < 0.3
    assert f0["peak_magnitude_db"] > f["peak_magnitude_db"]


def test_state_lists_each_presets_chirp_count():
    """The UI greys MTI out on single-chirp presets and CFAR on multi-chirp
    ones, before Start, from this."""
    h = _headless()
    info = h.get_fmcw_radar_state()["data"]["preset_info"]
    assert set(info) == set(dsp.FMCW_PRESETS)
    assert info["range_doppler"]["num_chirps"] > 1
    assert info["range_sync"]["num_chirps"] == 1


def test_presets_have_short_labels_in_ui_order():
    h = _headless()
    d = h.get_fmcw_radar_state()["data"]
    assert d["presets"][0] == dsp.FMCW_DEFAULT_PRESET == "range_doppler"
    assert set(d["presets"]) == set(dsp.FMCW_PRESETS)
    for name, info in d["preset_info"].items():
        assert info["label"] and len(info["label"]) <= 18, name  # fits the dropdown
        assert info["hint"], name


def test_default_falls_back_to_the_range_lab_without_chirp_sync():
    h = _headless()
    h.fmcw_capability = {**h.fmcw_capability, "ramp": dsp.RAMP_FREE_RUNNING}
    assert h._default_fmcw_preset() == dsp.FMCW_FALLBACK_PRESET == "range"
