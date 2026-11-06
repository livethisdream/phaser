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
    h.cw_lock = threading.RLock()  # as phaser_headless: re-entrant
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


def test_range_doppler_calibration_reads_the_still_reflector():
    """The peak readout skips 0 m/s, so with MTI off it follows the mover; the
    0 m calibration needs the still corner reflector at 1 m instead."""
    h = _headless()
    _start(h, preset="range_doppler", mti="none")
    frame = h.do_fmcw_radar_frame()
    assert frame["cal_peak_range"] == pytest.approx(1.0, abs=0.45)
    assert frame["peak_range"] == pytest.approx(3.5, abs=0.5)


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


def test_state_gives_each_presets_hardware_for_the_pane():
    """The Hardware pane follows the preset in FMCW mode: it shows the lab's
    own output frequency and gains, not CW's."""
    h = _headless()
    info = h.get_fmcw_radar_state()["data"]["preset_info"]
    for name, i in info.items():
        cfg = dsp.fmcw_config(preset=name)
        assert (i["output_freq"], i["rx_gain"], i["tx_gain"]) == \
            (cfg["output_freq"], cfg["rx_gain"], cfg["tx_gain"]), name


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


# --- Save Capture: the course .npz -------------------------------------------

def _saved(h, **params):
    import base64
    import io
    resp = h.save_fmcw_capture(params)
    assert resp["status"] == "ok", resp
    with np.load(io.BytesIO(base64.b64decode(resp["data"])), allow_pickle=False) as z:
        cap = {k: z[k] for k in z.files}
    return resp, cap


def _course_peak_range(cap, r_min=0.3):
    """The strongest return by the course's own formula (radar_lab.range_axis):
    R = c T_ramp (f - f0) / (2 B), with its c = 3.00e8."""
    iq = cap["iq"]
    n = iq.shape[1]
    w = np.hanning(n)
    sp = np.abs(np.fft.fft(iq * w, axis=1)).mean(axis=0)
    f = np.fft.fftfreq(n, 1 / float(cap["fs"]))
    r = (f - float(cap["f0"])) * 3.00e8 * float(cap["T_ramp"]) / (2 * float(cap["B"]))
    keep = (r >= r_min) & (r < 10)
    return r[keep][np.argmax(sp[keep])]


def test_save_capture_is_the_course_npz_for_a_burst():
    """radar_lab.load_capture's contract: iq is n_chirps x n_samples complex,
    fs fc B T_ramp PRI f0 are scalars that convert to float, SI units."""
    h = _headless()
    _start(h, preset="range_doppler")
    resp, cap = _saved(h)
    assert set(cap) == {"iq", "fs", "fc", "B", "T_ramp", "PRI", "f0", "note"}
    assert cap["iq"].dtype == np.complex64
    assert cap["iq"].shape == (h.fmcw_plan["num_chirps"], h.fmcw_plan["good_ramp_samples"])
    for k in ("fs", "fc", "B", "T_ramp", "PRI", "f0"):
        assert cap[k].shape == () and float(cap[k]) > 0, k
    assert float(cap["fs"]) == h.fmcw_plan["sample_rate"]
    assert float(cap["fc"]) == h.fmcw_cfg["output_freq"]
    assert float(cap["B"]) == h.fmcw_cfg["chirp_bw"]
    assert float(cap["T_ramp"]) == h.fmcw_plan["effective_ramp_time"]
    assert float(cap["PRI"]) == pytest.approx(h.fmcw_plan["pri_ms"] / 1e3)
    assert float(cap["f0"]) == h.fmcw_cfg["signal_freq"]   # not calibrated: the IF tone
    assert "IF tone" in str(cap["note"])
    assert resp["n_chirps"] == cap["iq"].shape[0] and resp["size"] > 0
    # The sim's corner reflector is at 1 m, read the course's way.
    assert _course_peak_range(cap) == pytest.approx(1.0, abs=0.3)


def test_save_capture_on_a_single_chirp_preset_saves_32_frames():
    h = _headless()
    _start(h, preset="range")
    _, cap = _saved(h)
    assert cap["iq"].shape == (dsp.CAPTURE_SINGLE_CHIRP_FRAMES, h.fmcw_plan["good_ramp_samples"])
    assert float(cap["PRI"]) > 0       # the measured frame interval
    assert _course_peak_range(cap) == pytest.approx(1.0, abs=0.3)


def test_save_capture_carries_the_0m_calibration_as_f0():
    h = _headless()
    _start(h, preset="range_doppler", zero_range_freq=103_500.0)
    _, cap = _saved(h)
    assert float(cap["f0"]) == 103_500.0
    assert "0 m calibration" in str(cap["note"])


def test_save_capture_with_iq_ch_saves_both_halves():
    h = _headless()
    _start(h, preset="range_doppler")
    _, cap = _saved(h, iq_ch=True)
    assert cap["iq_ch"].shape == (2,) + cap["iq"].shape
    assert cap["iq_ch"].dtype == np.complex64
    np.testing.assert_allclose(cap["iq_ch"].sum(axis=0), cap["iq"], atol=1e-3)


def test_save_capture_needs_fmcw_running():
    h = _headless()
    assert h.save_fmcw_capture({})["status"] == "error"


# --- Save Capture: duration ----------------------------------------------------

def test_capture_limits_per_preset():
    """What the Duration box offers. Range-Doppler presets save runs of
    ordinary bursts, bounded by CAPTURE_MAX_BYTES; L31 alone may save one
    continuous burst (in sim; on a kit only with PHASER_LONG_BURST=1), bounded
    by the Pluto's buffer; a single-chirp preset by CAPTURE_MAX_FRAMES_S."""
    h = _headless()
    caps = {}
    for preset in ("range_doppler", "range_doppler_far", "l31_record", "range"):
        h.fmcw_params = {"preset": preset}
        caps[preset] = h.get_fmcw_radar_state()["data"]["capture"]
    rd = caps["range_doppler"]
    assert rd["kind"] == "bursts" and rd["run_s"] == dsp.CAPTURE_BURST_INTERVAL_S
    assert dsp.capture_bytes(rd, rd["max_s"]) <= dsp.CAPTURE_MAX_BYTES
    assert caps["range_doppler_far"]["max_s"] < rd["max_s"]      # twice the samples
    assert caps["l31_record"]["kind"] == "burst" and caps["l31_record"]["max_s"] > 10
    assert caps["range"]["kind"] == "frames" and caps["range"]["max_s"] == dsp.CAPTURE_MAX_FRAMES_S
    for c in caps.values():
        assert c["bytes_per_s_iq_ch"] == pytest.approx(3 * c["bytes_per_s"])


def test_save_capture_shorter_than_a_burst_trims_it():
    h = _headless()
    _start(h, preset="range_doppler")
    _, cap = _saved(h, duration_s=0.05)
    assert cap["iq"].shape[0] == 100            # 0.05 s / 0.5 ms
    assert "burst_len" not in cap               # one burst: continuous


def test_save_capture_longer_than_a_burst_is_a_run_of_bursts():
    """No re-entry, no giant buffer: whole live bursts, run_s apart, and the
    file says where each one starts."""
    h = _headless()
    _start(h, preset="range_doppler", scene="walker")
    before = dict(h.fmcw_plan)
    resp, cap = _saved(h, duration_s=1.0)
    n = before["num_chirps"]
    assert cap["iq"].shape == (4 * n, before["good_ramp_samples"])    # 1 s / 0.25 s
    assert float(cap["burst_len"]) == n
    assert cap["burst_t"].shape == (4,) and cap["burst_t"][0] == 0
    assert np.diff(cap["burst_t"]) == pytest.approx(dsp.CAPTURE_BURST_INTERVAL_S, rel=0.3)
    assert float(cap["PRI"]) == pytest.approx(before["pri_ms"] / 1e3)   # within a burst
    lim = h.get_fmcw_radar_state()["data"]["capture"]
    assert resp["size"] == pytest.approx(dsp.capture_bytes(lim, 1.0), rel=0.01)
    assert "4 bursts" in str(cap["note"])
    # Nothing was re-entered: the same plan throughout.
    assert h.mode == "fmcw_radar" and h.fmcw_plan == before


def test_long_save_on_a_kit_is_never_one_continuous_burst(monkeypatch):
    """Off a sim, even L31 saves a run of bursts unless PHASER_LONG_BURST=1."""
    h = _headless()
    monkeypatch.delenv("PHASER_LONG_BURST", raising=False)
    assert h._capture_continuous("l31_record") is True        # sim
    assert h._capture_continuous("range_doppler") is False
    h.sim_mode = False
    assert h._capture_continuous("l31_record") is False
    monkeypatch.setenv("PHASER_LONG_BURST", "1")
    assert h._capture_continuous("l31_record") is True
    assert h._capture_continuous("range_doppler") is False


def test_save_capture_refuses_more_than_its_limit():
    h = _headless()
    _start(h, preset="range_doppler")
    lim = h.get_fmcw_radar_state()["data"]["capture"]
    resp = h.save_fmcw_capture({"duration_s": lim["max_s"] + 1})
    assert resp["status"] == "error" and f"{lim['max_s']:.3g} s" in resp["message"]
    assert h.mode == "fmcw_radar" and h.fmcw_plan["num_chirps"] == 256


def test_l31_continuous_burst_is_re_entered_and_put_back():
    """L31's one continuous burst (sim): re-entered for it, then put back."""
    h = _headless()
    _start(h, preset="l31_record", scene="walker")
    before = dict(h.fmcw_plan)
    resp, cap = _saved(h, duration_s=1.0)
    assert cap["iq"].shape == (2000, before["good_ramp_samples"]) and "burst_len" not in cap
    assert h.mode == "fmcw_radar" and h.fmcw_plan["num_chirps"] == before["num_chirps"]
    assert h.fmcw_preset == "l31_record" and h.fmcw_cfg.get("scene") == "walker"
    assert "num_chirps" not in h.fmcw_params


def test_l31_record_preset_saves_seconds_with_iq_ch():
    h = _headless()
    _start(h, preset="l31_record")
    resp, cap = _saved(h, duration_s=3.0, iq_ch=True)
    assert cap["iq"].shape[0] == 6000 and float(cap["fs"]) == 300_000.0
    assert cap["iq_ch"].shape == (2,) + cap["iq"].shape
    lim = h.get_fmcw_radar_state()["data"]["capture"]
    assert resp["size"] == pytest.approx(lim["bytes_per_s_iq_ch"] * 3.0, rel=0.01)


def test_single_chirp_save_takes_frames_at_the_frame_interval():
    h = _headless()
    _start(h, preset="range")
    _, cap = _saved(h, duration_s=0.5)
    assert cap["iq"].shape[0] == 10            # 0.5 s / 50 ms
    assert float(cap["PRI"]) == pytest.approx(dsp.CAPTURE_FRAME_S, rel=0.2)



@pytest.mark.parametrize("preset", ["range", "range_sync"])
def test_one_chirp_save_stores_the_frame_interval_as_pri(preset):
    """One frame has no interval to measure; the next chirp would have come a
    frame interval (50 ms) later, not the plan's 2 ms / 1.5 ms PRI."""
    h = _headless()
    _start(h, preset=preset)
    _, cap = _saved(h, duration_s=dsp.CAPTURE_FRAME_S)
    assert cap["iq"].shape[0] == 1
    assert float(cap["PRI"]) == dsp.CAPTURE_FRAME_S


def test_save_capture_is_in_full_scale_units():
    """The course's units: |iq| = 1 is 0 dBFS. The sim's strongest return
    sits well under full scale, so its peak is a fraction, not ~500 counts."""
    h = _headless()
    _start(h, preset="range")
    _, cap = _saved(h)
    peak = float(np.max(np.abs(cap["iq"])))
    assert 1e-4 < peak < 1.0, peak


# --- scene noise (simulation only) -------------------------------------

def test_scene_noise_levels_are_offered_and_default_to_clean():
    import phaser_sim
    h = _headless()
    _start(h, scene="walker")
    d = h.get_fmcw_radar_state()["data"]
    assert set(d["scene_noise_levels"]) == set(phaser_sim.SCENE_NOISE) == {"clean", "typical", "noisy"}
    assert d["scene_noise"] == "clean"
    assert h.sdr._fmcw_scene_noise == "clean"


def test_clean_is_the_course_scene_and_static_ignores_noise():
    import phaser_sim as ps
    t = np.linspace(0, 2, 7)
    for name in ("corners", "walker", "hand"):
        clean = ps.scene_scatterers(name, t, 2e6, 10e9, "clean")
        assert len(clean) == len(ps._scene_targets(name, t, 2e6, 10e9))
        noisy = ps.scene_scatterers(name, t, 2e6, 10e9, "typical")
        assert len(noisy) == len(clean) + len(ps.SCENE_SKIRT) + len(ps.SCENE_CLUTTER)
    assert len(ps.scene_scatterers("static", t, 2e6, 10e9, "noisy")) == 1
    with pytest.raises(ValueError):
        ps.scene_scatterers("walker", t, 2e6, 10e9, "bogus")


def test_fading_stays_within_its_depth_and_corners_hold_steady():
    import phaser_sim as ps
    t = np.linspace(0, 10, 4001)
    for k in range(5):
        f = ps.scene_fluct_db(t, k, 6.0)
        assert np.all(np.abs(f) <= 6.0)
        assert f.max() - f.min() > 6.0          # it does fade, and by several dB
    clean = ps.scene_scatterers("corners", t, 2e6, 10e9, "clean")
    noisy = ps.scene_scatterers("corners", t, 2e6, 10e9, "noisy")
    for (_, a, _, _), (_, b, _, _) in zip(clean, noisy):
        assert np.array_equal(a, b)


def test_noisier_levels_raise_the_floor():
    """Median of the range profile (mostly noise bins) climbs with the level."""
    medians = {}
    for level in ("clean", "typical", "noisy"):
        h = _headless()
        _start(h, preset="range", scene="hand", scene_noise=level)
        medians[level] = float(np.median(h.do_fmcw_radar_frame()["profile_db"]))
    assert medians["typical"] > medians["clean"] + 6, medians
    assert medians["noisy"] > medians["typical"] + 4, medians


def test_scene_noise_changes_live_without_reentering():
    h = _headless()
    _start(h, scene="walker", scene_noise="clean")
    resp = h.set_fmcw_radar_params({"scene_noise": "noisy"})
    assert resp["status"] == "ok" and resp["reentered"] is False
    assert h.sdr._fmcw_scene_noise == "noisy"
    assert h.get_fmcw_radar_state()["data"]["scene_noise"] == "noisy"
    with pytest.raises(ValueError):
        h.sdr.set_fmcw_scene_noise("bogus")
