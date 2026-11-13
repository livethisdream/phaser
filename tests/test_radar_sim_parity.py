"""The browser radar simulator must agree with the Python one.

frontend-radar/src/sim/ is a hand port of the radar half of phaser_sim.py, the
phaser_radar_dsp functions the frames are built from, and the radar handlers in
phaser_headless.py. It is what runs the radar UI with ?sim=1 and on the GitHub
Pages demo, where there is no backend. When the two drift, the demo quietly
stops showing what a kit shows; this is the test that makes drift fail.

Constants are covered separately and more strongly: tools/gen_sim_constants.py
generates frontend-radar/src/sim/constants.generated.js and CI fails when it is
stale. What is left here is behavioral drift.

As in tests/test_sim_parity.py, both sides run with the noise off -- NumPy's
PCG64 stream cannot be reproduced in JS -- and the deterministic physics is
compared frame for frame. phaser_sim synthesizes in complex64 and the JS in
float64, so dB values agree to a few thousandths, not exactly; the encoded
range-Doppler map, one byte a cell, may differ by one step where a cell sits on
a quantization boundary.

Parity is only as strong as CASES. A new radar knob belongs here.
"""

import base64
import json
import shutil
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

np = pytest.importorskip("numpy")

NODE = shutil.which("node")

# (name, mode, start params). Between them: every preset, both MTI modes, CFAR,
# the window, taper, 0 m calibration, range and velocity crops, and the CW
# capture knobs.
CASES = [
    ("rd_default", "fmcw", {}),
    ("rd_mti_2pulse", "fmcw", {"mti": "2pulse"}),
    ("rd_mti_3pulse", "fmcw", {"mti": "3pulse"}),
    ("rd_far", "fmcw", {"preset": "range_doppler_far"}),
    ("rd_hann_calibrated_cropped", "fmcw",
     {"fft_window": "hann", "zero_range_freq": 103_000.0, "range_max": 12.0}),
    ("rd_vel_crop_no_exclusion", "fmcw", {"vel_max": 3.0, "peak_exclude": 0}),
    ("rd_rect_taper_hamming", "fmcw", {"taper": "rect", "fft_window": "hamming"}),
    ("range", "fmcw", {"preset": "range"}),
    ("range_sync", "fmcw", {"preset": "range_sync"}),
    ("cfar", "fmcw", {"preset": "cfar", "cfar": True}),
    ("cfar_tuned", "fmcw",
     {"preset": "cfar", "cfar": True, "cfar_guard": 2, "cfar_ref": 8, "cfar_bias_db": 3.0}),
    ("range_rect_window", "fmcw", {"preset": "range", "fft_window": "rect", "taper": "hann"}),
    # The micro-Doppler spectrogram held at a range, with and without MTI.
    ("rd_spectrogram_fixed", "fmcw", {"spectrogram_range": 3.5}),
    ("rd_spectrogram_mti", "fmcw", {"spectrogram_range": 3.5, "mti": "2pulse"}),
    # Peak Min off, and a 0 m beat set above the reflector's (Calibrate has
    # to find it below 0 m, outside the displayed profile).
    ("range_peak_min_off", "fmcw", {"preset": "range", "peak_min": 0}),
    ("range_peak_min_far", "fmcw", {"preset": "range", "peak_min": 5.0}),
    ("range_cal_below_zero", "fmcw", {"preset": "range", "zero_range_freq": 107_000.0}),
    ("rd_cal_below_zero", "fmcw", {"zero_range_freq": 130_000.0}),
    ("cw_default", "cw", {}),
    ("cw_hann_rect_taper", "cw", {"fft_window": "hann", "taper": "rect", "vel_max": 10.0}),
    ("cw_capture", "cw",
     {"sample_rate": 1_000_000, "fft_size": 16384, "peak_exclude": 0}),
    ("cw_carrier", "cw", {"signal_freq": 150_000, "output_freq": 10e9}),
]
CASE_IDS = [c[0] for c in CASES]

# dB agreement, complex64 against float64 synthesis (see the docstring).
DB_TOL = 0.02
AXIS_TOL = 1e-3
# dB traces are compared only this far below their peak. Further down, both
# sides are at complex64's rounding floor (~140 dB below the peak), where the
# Python synthesis produces numerical garbage that differs by tenths of a dB
# 110-190 dB down: far below any receiver's noise, and nothing a plot shows.
DB_FLOOR_BELOW_PEAK = 90.0


def _python_frame(mode, params, clock=None):
    if "adi" not in sys.modules:
        sys.modules["adi"] = types.ModuleType("adi")
    import phaser_sim

    original = phaser_sim.SimSDR.NOISE_SIGMA
    phaser_sim.SimSDR.NOISE_SIGMA = 0.0
    try:
        import phaser_headless

        h = phaser_headless.PhaserHeadless.__new__(phaser_headless.PhaserHeadless)
        h.sim_mode = True
        h.c = 299792458
        h._do_init_hardware()
        h.mode = "idle"
        h.sweeping = False
        h.cw_params = {}
        h.cw_saved_sdr = {}
        h.cw_lock = threading.RLock()
        h.fmcw_params = {}
        h.fmcw_saved = {}
        h.fmcw_hw = None
        h.fmcw_cfg = None
        h.fmcw_plan = None
        h.cal_process = None
        if mode == "cw":
            assert h.start_cw_radar(dict(params))["status"] == "ok"
            return h.do_cw_radar_frame()
        if clock is not None:
            h.sdr.scene_clock = lambda: clock
        assert h.start_fmcw_radar(dict(params))["status"] == "ok"
        return h.do_fmcw_radar_frame()
    finally:
        phaser_sim.SimSDR.NOISE_SIGMA = original


_JS_DRIVER = """
import { createRadarEngine } from '%(sim)s/engine.js';
const cases = JSON.parse(process.argv[2]);
const out = {};
for (const [name, mode, params] of cases) {
    const engine = createRadarEngine({ noiseSigma: 0 });
    const resp = engine.handle(mode === 'cw' ? 'start_cw_radar' : 'start_fmcw_radar', params);
    if (resp.status !== 'ok') throw new Error(name + ': ' + JSON.stringify(resp));
    out[name] = engine.frame().data;
}
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def js_frames(tmp_path_factory):
    if NODE is None:
        pytest.skip("node is not installed")
    sim_dir = (ROOT / "frontend-radar" / "src" / "sim").as_posix()
    driver = tmp_path_factory.mktemp("radar_parity") / "driver.mjs"
    driver.write_text(_JS_DRIVER % {"sim": sim_dir})
    proc = subprocess.run(
        [NODE, str(driver), json.dumps([list(c) for c in CASES])],
        capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        raise AssertionError(f"node driver failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


def _decode(enc):
    q = np.frombuffer(base64.b64decode(enc["data"]), dtype=np.uint8)
    return q.reshape(enc["rows"], enc["cols"]).astype(int)


def _close(name, key, py, js, tol):
    py, js = np.asarray(py, dtype=float), np.asarray(js, dtype=float)
    assert py.shape == js.shape, f"{name}.{key}: shape {js.shape}, Python {py.shape}"
    if py.size:
        worst = float(np.max(np.abs(py - js)))
        assert worst <= tol, f"{name}.{key}: off by {worst:g} (tolerance {tol:g})"


@pytest.mark.parametrize("name,mode,params", CASES, ids=CASE_IDS)
def test_frame_matches_python(name, mode, params, js_frames):
    try:
        py = _python_frame(mode, params)
    except Exception as exc:  # noqa: BLE001 - zmq/msgpack absent
        if "No module named" in str(exc):
            pytest.skip(f"phaser_headless unavailable: {exc}")
        raise
    js = js_frames[name]

    ignore = {"capture_ms", "process_ms"}
    assert set(js) - ignore == set(py) - ignore, (
        f"{name}: keys differ: JS only {sorted(set(js) - set(py))}, "
        f"Python only {sorted(set(py) - set(js))}")

    for key, value in py.items():
        if key in ignore:
            continue
        other = js[key]
        if key in ("map", "sg_map") and value is not None:
            assert (other["rows"], other["cols"]) == (value["rows"], value["cols"]), name
            _close(name, "map.db_max", value["db_max"], other["db_max"], DB_TOL)
            _close(name, "map.db_min", value["db_min"], other["db_min"], DB_TOL)
            steps = np.abs(_decode(value) - _decode(other))
            assert steps.max() <= 1, f"{name}: map cells differ by {steps.max()} steps"
        elif key in ("detection_indices",):
            assert other == value, f"{name}.{key}: {other} != {value}"
        elif isinstance(value, str) or value is None or isinstance(value, bool):
            assert other == value, f"{name}.{key}: {other!r} != {value!r}"
        elif key.endswith("_db") and isinstance(value, list):
            a, b = np.asarray(value), np.asarray(other)
            assert a.shape == b.shape, f"{name}.{key}: shape {b.shape}, Python {a.shape}"
            keep = a > a.max() - DB_FLOOR_BELOW_PEAK
            _close(name, key, a[keep], b[keep], DB_TOL)
        elif key.endswith("_db"):
            _close(name, key, value, other, DB_TOL)
        else:
            # Axes, ranges, velocities, counts: physics, so tight.
            tol = AXIS_TOL if isinstance(value, list) else max(AXIS_TOL, 1e-6 * abs(value))
            if key == "iq_mag":
                tol = 1e-3 * max(1.0, float(np.max(np.abs(value))))
            _close(name, key, value, other, tol)


# --- Save Capture -------------------------------------------------------------
#
# The browser simulator writes the course .npz itself (frontend-radar/src/npz.js),
# so a sim cadet's saved file has to be the file a kit or a --sim backend saves:
# the same keys, dtypes, shapes, scalars and samples. The note and, on a
# single-chirp preset, the measured frame interval (PRI) are allowed to differ.

SAVE_CASES = [
    ("save_rd", {}, False),
    ("save_rd_iq_ch_calibrated", {"zero_range_freq": 103_000.0}, True),
    ("save_range", {"preset": "range"}, False),
    # Duration: a burst trimmed, a long continuous burst, Micro-Doppler's long record.
    ("save_rd_short", {"duration_s": 0.05}, False),
    ("save_rd_run", {"duration_s": 0.5}, False),             # a run of 2 bursts
    ("save_rd_run_iq_ch", {"duration_s": 1.0}, True),        # 4 bursts, both halves
    ("save_micro_doppler_long_iq_ch", {"preset": "micro_doppler", "duration_s": 1.5}, True),
    # One single chirp: PRI is the frame interval, not the plan's.
    ("save_range_one_chirp", {"preset": "range_sync", "duration_s": 0.05}, False),
]

_JS_SAVE_DRIVER = """
import { createRadarEngine } from '%(sim)s/engine.js';
const cases = JSON.parse(process.argv[2]);
const out = {};
for (const [name, params, iqCh] of cases) {
    const engine = createRadarEngine({ noiseSigma: 0 });
    const { duration_s, ...start } = params;
    engine.handle('start_fmcw_radar', start);
    const resp = engine.handle('save_fmcw_capture', { iq_ch: iqCh, duration_s });
    if (resp.status !== 'ok') throw new Error(name + ': ' + JSON.stringify(resp));
    out[name] = resp.data;
}
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def js_saves(tmp_path_factory):
    if NODE is None:
        pytest.skip("node is not installed")
    sim_dir = (ROOT / "frontend-radar" / "src" / "sim").as_posix()
    driver = tmp_path_factory.mktemp("radar_save_parity") / "driver.mjs"
    driver.write_text(_JS_SAVE_DRIVER % {"sim": sim_dir})
    proc = subprocess.run([NODE, str(driver), json.dumps([list(c) for c in SAVE_CASES])],
                          capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise AssertionError(f"node driver failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


def _load_npz(b64):
    import io
    with np.load(io.BytesIO(base64.b64decode(b64)), allow_pickle=False) as z:
        return {k: z[k] for k in z.files}


def _python_save(params, iq_ch):
    if "adi" not in sys.modules:
        sys.modules["adi"] = types.ModuleType("adi")
    import phaser_sim

    original = phaser_sim.SimSDR.NOISE_SIGMA
    phaser_sim.SimSDR.NOISE_SIGMA = 0.0
    try:
        import phaser_headless

        h = phaser_headless.PhaserHeadless.__new__(phaser_headless.PhaserHeadless)
        h.sim_mode = True
        h.c = 299792458
        h._do_init_hardware()
        h.mode, h.sweeping = "idle", False
        h.cw_params, h.cw_saved_sdr, h.cw_lock = {}, {}, threading.RLock()
        h.fmcw_params, h.fmcw_saved, h.fmcw_hw = {}, {}, None
        h.fmcw_cfg = h.fmcw_plan = h.cal_process = None
        start = {k: v for k, v in params.items() if k != "duration_s"}
        assert h.start_fmcw_radar(start)["status"] == "ok"
        resp = h.save_fmcw_capture({"iq_ch": iq_ch, "duration_s": params.get("duration_s")})
        assert resp["status"] == "ok", resp
        return resp["data"]
    finally:
        phaser_sim.SimSDR.NOISE_SIGMA = original


@pytest.mark.parametrize("name,params,iq_ch", SAVE_CASES, ids=[c[0] for c in SAVE_CASES])
def test_saved_capture_matches_python(name, params, iq_ch, js_saves):
    py = _load_npz(_python_save(params, iq_ch))
    js = _load_npz(js_saves[name])
    assert set(js) == set(py), f"{name}: keys {sorted(js)} vs Python {sorted(py)}"
    single_chirp = params.get("preset") in ("range", "range_sync", "cfar")
    for key, value in py.items():
        other = js[key]
        assert other.dtype.kind == value.dtype.kind and other.shape == value.shape, (
            f"{name}.{key}: {other.dtype}{other.shape} vs Python {value.dtype}{value.shape}")
        if key == "note" or (key == "PRI" and single_chirp):
            continue
        if key == "burst_t":       # Python measures the run's pacing; JS steps it
            np.testing.assert_allclose(other, value, atol=0.1)
            continue
        if value.dtype.kind == "c":
            scale = float(np.max(np.abs(value)))
            worst = float(np.max(np.abs(value - other)))
            assert worst <= 1e-4 * scale, f"{name}.{key}: samples off by {worst:g} of {scale:g}"
        else:
            assert float(other) == pytest.approx(float(value), rel=1e-12), f"{name}.{key}"


# --- Load Recording: reading and playing a course .npz in the browser ----------
#
# frontend-radar/src/npz.js reads the course's captures and playback-worker.js
# plays them through dsp.fmcwPayload(). The reader has to read what numpy
# writes -- np.savez and np.savez_compressed, complex64 and complex128, the
# scalars and the note -- and a played frame has to be what the Python
# processing makes of the same chirps.

_JS_READ_DRIVER = """
import { readNpz } from '%(src)s/npz.js';
import { readFileSync } from 'node:fs';
const out = {};
for (const path of JSON.parse(process.argv[2])) {
    const z = await readNpz(readFileSync(path));
    out[path] = Object.fromEntries(Object.entries(z).map(([k, v]) => [k, {
        dtype: v.dtype, shape: v.shape,
        data: v.data && v.data.re ? { re: Array.from(v.data.re), im: Array.from(v.data.im) }
            : ArrayBuffer.isView(v.data) ? Array.from(v.data) : v.data,
    }]));
}
process.stdout.write(JSON.stringify(out));
"""


def _node(driver_src, arg, tmp_path):
    if NODE is None:
        pytest.skip("node is not installed")
    driver = tmp_path / "driver.mjs"
    driver.write_text(driver_src)
    proc = subprocess.run([NODE, str(driver), json.dumps(arg)],
                          capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise AssertionError(f"node driver failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


@pytest.mark.parametrize("compressed", [False, True], ids=["savez", "savez_compressed"])
@pytest.mark.parametrize("cdtype", [np.complex64, np.complex128])
def test_browser_reads_what_numpy_writes(compressed, cdtype, tmp_path):
    rng = np.random.default_rng(1)
    iq = (rng.standard_normal((5, 7)) + 1j * rng.standard_normal((5, 7))).astype(cdtype)
    path = tmp_path / "cap.npz"
    (np.savez_compressed if compressed else np.savez)(
        path, iq=iq, fs=np.float64(4e6), f0=np.float64(101400.0), PRI=0.0005,
        note=np.str_("a corner at 1.0 m, ± 0.1"), truth_t=np.arange(3, dtype=np.float64))
    src = (ROOT / "frontend-radar" / "src").as_posix()
    got = _node(_JS_READ_DRIVER % {"src": src}, [str(path)], tmp_path)[str(path)]
    assert got["iq"]["shape"] == [5, 7]
    js_iq = np.array(got["iq"]["data"]["re"]) + 1j * np.array(got["iq"]["data"]["im"])
    np.testing.assert_allclose(js_iq, iq.ravel(), rtol=1e-6, atol=1e-6)
    assert got["fs"]["data"] == 4e6 and got["f0"]["data"] == 101400.0 and got["PRI"]["data"] == 0.0005
    assert got["note"]["data"] == "a corner at 1.0 m, ± 0.1"
    assert got["truth_t"]["data"] == [0.0, 1.0, 2.0]


_JS_PLAY_DRIVER = """
import { readNpz } from '%(src)s/npz.js';
import { playbackRecording, playbackFrame } from '%(src)s/playback-worker.js';
import { readFileSync } from 'node:fs';
const [path, settings] = JSON.parse(process.argv[2]);
const rec = playbackRecording(await readNpz(readFileSync(path)));
const f = playbackFrame(rec, settings, 0);
process.stdout.write(JSON.stringify({ mode: rec.mode, window: rec.window, units: rec.units, frame: f }));
"""


@pytest.mark.parametrize("preset,settings", [
    ("range_doppler", {}),
    ("range_doppler", {"mti": "2pulse", "fft_window": "hann"}),
    ("range", {"cfar": True}),
], ids=["rd", "rd_mti_hann", "range_cfar"])
def test_playback_frame_matches_python_on_a_saved_capture(preset, settings, tmp_path):
    """Save a capture from a --sim backend, play it in the browser: the frame
    is what phaser_radar_dsp makes of the same chirps with the same settings."""
    import phaser_radar_dsp as dsp

    data = _python_save({"preset": preset}, False)
    path = tmp_path / "cap.npz"
    path.write_bytes(base64.b64decode(data))
    cap = _load_npz(data)
    src = (ROOT / "frontend-radar" / "src").as_posix()
    got = _node(_JS_PLAY_DRIVER % {"src": src}, [str(path), settings], tmp_path)
    f = got["frame"]

    # Saved in full-scale units; playback scales back to the counts the DSP reads.
    assert got["units"] == "full_scale"
    assert np.max(np.abs(cap["iq"])) < dsp.CAPTURE_COUNTS_ABOVE
    iq = cap["iq"] * dsp.CAPTURE_FULL_SCALE
    fs, B, T, PRI, fc, f0 = (float(cap[k]) for k in ("fs", "B", "T_ramp", "PRI", "fc", "f0"))
    window = settings.get("fft_window", "blackman")
    if iq.shape[0] > 1 and PRI <= 5e-3:
        assert got["mode"] == "burst" and f["kind"] == "range_doppler"
        matrix = dsp.mti_filter(iq.T, settings.get("mti", "none"))
        rd = dsp.range_doppler_map(matrix, fs, B, T, PRI, fc, zero_range_freq=f0,
                                   fft_window=window, range_max=20.0, peak_exclude=0.3)
        assert f["peak_range"] == pytest.approx(rd["peak_range"], abs=1e-6)
        assert f["peak_velocity"] == pytest.approx(rd["peak_velocity"], abs=1e-6)
        assert f["cal_peak_range"] == pytest.approx(rd["cal_peak_range"], abs=1e-6)
        enc = dsp.encode_db_u8(rd["map_db"])
        steps = np.abs(_decode(f["map"]) - _decode(enc))
        assert steps.max() <= 1
    else:
        assert got["mode"] == "profile" and f["kind"] == "range_profile"
        prof = dsp.range_profile(iq[0], fs, B, T, zero_range_freq=f0, fft_window=window,
                                 range_max=20.0)
        assert f["peak_range"] == pytest.approx(prof["peak_range"], abs=1e-6)
        a, b = np.asarray(prof["profile_db"]), np.asarray(f["profile_db"])
        keep = a > a.max() - DB_FLOOR_BELOW_PEAK
        assert np.max(np.abs(a[keep] - b[keep])) <= DB_TOL
        if settings.get("cfar"):
            thr = dsp.ca_cfar(np.asarray(prof["profile_db"]))
            _, det = dsp.apply_cfar(np.asarray(prof["profile_db"]), thr)
            assert f["detection_indices"] == [int(i) for i in det]
    # The sim's stationary reflector is at 1 m -- unless MTI cancelled it,
    # which is what MTI is for.
    if settings.get("mti", "none") == "none":
        assert f["cal_peak_range"] == pytest.approx(1.0, abs=0.35)




def _course_file(path, iq, **extra):
    """A file as the course writes them: full-scale units, f0 at the IF."""
    np.savez(path, iq=iq.astype(np.complex64), fs=np.float64(2e6), fc=np.float64(10e9),
             B=np.float64(500e6), T_ramp=np.float64(300e-6), PRI=np.float64(50e-3),
             f0=np.float64(100e3), **extra)


def _tone(r, level_dbfs, n=600, fs=2e6, B=500e6, T=300e-6, f0=100e3):
    """One chirp: a target at r meters, level_dbfs per sample (|x| = 1 is 0 dBFS)."""
    f = f0 + 2 * B * r / (299792458.0 * T)
    return 10 ** (level_dbfs / 20) * np.exp(2j * np.pi * f * np.arange(n) / fs)


@pytest.mark.parametrize("scale,units", [(1.0, "full_scale"), (2.0 ** 11, "counts")],
                         ids=["full_scale", "old_counts"])
def test_playback_reads_course_levels_in_dbfs(scale, units, tmp_path):
    """A course file's levels play at their dBFS, not 66 dB low; an older
    save in raw counts is recognized and plays at the same level."""
    iq = np.stack([_tone(2.0, -20.0)] * 4) * scale
    path = tmp_path / "course.npz"
    _course_file(path, iq)
    src = (ROOT / "frontend-radar" / "src").as_posix()
    got = _node(_JS_PLAY_DRIVER % {"src": src}, [str(path), {"peak_min": 0}], tmp_path)
    assert got["units"] == units
    f = got["frame"]
    assert f["peak_range"] == pytest.approx(2.0, abs=0.35)
    # A coherent tone of -20 dBFS a sample reads -20 dB once the window's sum
    # is divided out: the same scale as the live frames.
    assert f["peak_magnitude_db"] == pytest.approx(-20.0, abs=1.5)



_JS_RUN_DRIVER = """
import { readNpz } from '%(src)s/npz.js';
import { playbackRecording, playbackFrame } from '%(src)s/playback-worker.js';
import { readFileSync } from 'node:fs';
const [path] = JSON.parse(process.argv[2]);
const rec = playbackRecording(await readNpz(readFileSync(path)));
const second = playbackFrame(rec, {}, rec.hop);
process.stdout.write(JSON.stringify({ mode: rec.mode, window: rec.window, hop: rec.hop,
    interval: rec.interval, bursts: rec.bursts, second }));
"""


def test_a_run_of_bursts_plays_a_burst_a_frame(tmp_path):
    """A Range + Velocity save longer than one burst plays back burst by
    burst, at the recorded pace, each frame what Python makes of that burst."""
    import phaser_radar_dsp as dsp

    data = _python_save({"preset": "range_doppler", "duration_s": 1.0}, False)
    path = tmp_path / "run.npz"
    path.write_bytes(base64.b64decode(data))
    cap = _load_npz(data)
    n = int(cap["burst_len"])
    src = (ROOT / "frontend-radar" / "src").as_posix()
    got = _node(_JS_RUN_DRIVER % {"src": src}, [str(path)], tmp_path)
    assert got["mode"] == "burst" and got["window"] == got["hop"] == n
    assert got["bursts"] == cap["iq"].shape[0] // n == 4
    assert got["interval"] == pytest.approx(dsp.CAPTURE_BURST_INTERVAL_S * 1e3, rel=0.3)
    burst = cap["iq"][n:2 * n] * dsp.CAPTURE_FULL_SCALE
    rd = dsp.range_doppler_map(burst.T, float(cap["fs"]), float(cap["B"]), float(cap["T_ramp"]),
                               float(cap["PRI"]), float(cap["fc"]), zero_range_freq=float(cap["f0"]),
                               range_max=20.0, peak_exclude=0.3)
    assert got["second"]["peak_range"] == pytest.approx(rd["peak_range"], abs=1e-6)
    steps = np.abs(_decode(got["second"]["map"]) - _decode(dsp.encode_db_u8(rd["map_db"])))
    assert steps.max() <= 1


L28_CORNER = Path("/home/user/livethisdream/ece444/book/extras/radar-data/L28_corner_3m.npz")


@pytest.mark.skipif(not L28_CORNER.exists(), reason="the ece444 recordings are not checked out")
@pytest.mark.parametrize("peak_min,expect", [(None, 3.0), (0, 0.0)], ids=["default_1m", "off"])
def test_l28_corner_reads_the_corner_not_the_leakage(peak_min, expect, tmp_path):
    """The L28 thread's file: the 3 m corner is weaker than the leakage, so
    with Peak Min off the readout said the leakage. The 1 m default finds
    it, and peak interpolation puts it at its 3 m, not the 3.09 m bin."""
    src = (ROOT / "frontend-radar" / "src").as_posix()
    settings = {} if peak_min is None else {"peak_min": peak_min}
    got = _node(_JS_PLAY_DRIVER % {"src": src}, [str(L28_CORNER), settings], tmp_path)
    assert got["units"] == "full_scale"
    assert got["frame"]["peak_range"] == pytest.approx(expect, abs=0.05)
    # Calibrate reads the strongest return wherever it is: the leakage.
    assert got["frame"]["cal_peak_range"] == pytest.approx(0.0, abs=0.05)


# --- Live scenes ---------------------------------------------------------------
#
# The moving scenes (phaser_sim.FMCW_SCENES, after ece444 fmcw_sim.py) take the
# absolute time from a clock, so both sides run on the same fixed one. Several
# instants per scene: a scene that moved on one side and not the other, or the
# wrong way, fails at the times where it differs.

SCENE_CASES = [
    (f"{scene}_{preset}_t{t}", scene, preset, t, "clean")
    for scene in ("corners", "walker", "hand")
    for preset in ("range_doppler", "range")
    for t in (0.0, 0.8, 2.6)
] + [
    # Scene noise: the clutter, skirt and fading are deterministic, so they
    # compare frame for frame like the targets (the floor is off here).
    (f"{scene}_{preset}_t{t}_{noise}", scene, preset, t, noise)
    for noise in ("typical", "noisy")
    for scene in ("corners", "walker", "hand")
    for preset in ("range_doppler", "range")
    for t in (0.8, 2.6)
]

_JS_SCENE_DRIVER = """
import { createRadarEngine } from '%(sim)s/engine.js';
const cases = JSON.parse(process.argv[2]);
const out = {};
for (const [name, scene, preset, t, scene_noise] of cases) {
    const engine = createRadarEngine({ noiseSigma: 0, clock: () => t });
    const resp = engine.handle('start_fmcw_radar', { preset, scene, scene_noise });
    if (resp.status !== 'ok') throw new Error(name + ': ' + JSON.stringify(resp));
    out[name] = engine.frame().data;
}
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def js_scene_frames(tmp_path_factory):
    if NODE is None:
        pytest.skip("node is not installed")
    sim_dir = (ROOT / "frontend-radar" / "src" / "sim").as_posix()
    driver = tmp_path_factory.mktemp("radar_scene_parity") / "driver.mjs"
    driver.write_text(_JS_SCENE_DRIVER % {"sim": sim_dir})
    proc = subprocess.run([NODE, str(driver), json.dumps([list(c) for c in SCENE_CASES])],
                          capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise AssertionError(f"node driver failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


@pytest.mark.parametrize("name,scene,preset,t,noise", SCENE_CASES, ids=[c[0] for c in SCENE_CASES])
def test_scene_frame_matches_python(name, scene, preset, t, noise, js_scene_frames):
    py = _python_frame("fmcw", {"preset": preset, "scene": scene, "scene_noise": noise}, clock=t)
    js = js_scene_frames[name]
    # Corners hold still: with the noise off, the cells the moving-target peak
    # searches hold only rounding (~-300 dB), and which of two equal ones it
    # picks is not physics. Their map and stationary peak are still compared.
    moving = ("peak_range", "peak_velocity") if not (scene == "corners" and "map" in py) else ()
    for key in ("cal_peak_range",) + moving:
        if key in py:
            assert js[key] == pytest.approx(py[key], abs=AXIS_TOL), f"{name}.{key}"
    if "map" in py:
        steps = np.abs(_decode(py["map"]) - _decode(js["map"]))
        assert steps.max() <= 1, f"{name}: map cells differ by {steps.max()} steps"
        _close(name, "map.db_max", py["map"]["db_max"], js["map"]["db_max"], DB_TOL)
    else:
        a, b = np.asarray(py["profile_db"]), np.asarray(js["profile_db"])
        keep = a > a.max() - DB_FLOOR_BELOW_PEAK
        _close(name, "profile_db", a[keep], b[keep], DB_TOL)


def test_scenes_move():
    """The point of a live scene: frames at different times differ, and the
    walker's torso goes out and comes back."""
    r = [_python_frame("fmcw", {"preset": "range_doppler", "scene": "walker"}, clock=t)["peak_range"]
         for t in (0.0, 1.5, 3.0, 4.5)]
    assert r[0] < r[1] < r[2] and r[3] < r[2], r


# --- ISAR (docs/sar-plan.md, S1) ----------------------------------------------
# A sweep of isar_capture steps on the turntable scene, then the image, each
# side end to end. The scene's scatterers all hold still, so no clock is
# needed. Cells are compared down to ISAR_FLOOR_DB below the peak: in the
# nulls, complex64 against float64 rounding takes over.

ISAR_CASES = [
    ("isar_60_by_5", {"scene": "turntable"}, list(range(-30, 31, 5)), {}),
    ("isar_clutter_wide", {"scene": "turntable", "fft_window": "hann"},
     list(range(0, 91, 10)), {"clutter": True, "extent": 0.3, "r0": 1.45}),
    # Flip Phase, and a 0 m beat far enough off that the leakage sits
    # tenths of a meter from 0 m (the phase reference has to find it).
    ("isar_flip_offset_zero", {"scene": "turntable", "zero_range_freq": 104_000.0},
     list(range(-60, 61, 10)), {"flip": True}),
]
ISAR_FLOOR_DB = -30.0

_JS_ISAR_DRIVER = """
import { createRadarEngine } from '%(sim)s/engine.js';
const cases = JSON.parse(process.argv[2]);
const out = {};
for (const [name, params, angles, view] of cases) {
    const engine = createRadarEngine({ noiseSigma: 0 });
    let resp = engine.handle('start_fmcw_radar', params);
    if (resp.status !== 'ok') throw new Error(name + ': ' + JSON.stringify(resp));
    for (const a of angles) {
        resp = engine.handle('isar_capture', { angle_deg: a, ...view });
        if (resp.status !== 'ok') throw new Error(name + ': ' + JSON.stringify(resp));
    }
    const found = engine.handle('isar_find_distance', {});
    if (found.status !== 'ok') throw new Error(name + ': ' + JSON.stringify(found));
    out[name] = { ...resp.data, found_r0: found.data.r0 };
}
process.stdout.write(JSON.stringify(out));
"""


def _python_isar(params, angles, view):
    if "adi" not in sys.modules:
        sys.modules["adi"] = types.ModuleType("adi")
    import phaser_sim

    original = phaser_sim.SimSDR.NOISE_SIGMA
    phaser_sim.SimSDR.NOISE_SIGMA = 0.0
    try:
        import phaser_headless

        h = phaser_headless.PhaserHeadless.__new__(phaser_headless.PhaserHeadless)
        h.sim_mode = True
        h.c = 299792458
        h._do_init_hardware()
        h.mode = "idle"
        h.sweeping = False
        h.cw_params = {}
        h.cw_saved_sdr = {}
        h.cw_lock = threading.RLock()
        h.fmcw_params = {}
        h.fmcw_saved = {}
        h.fmcw_hw = None
        h.fmcw_cfg = None
        h.fmcw_plan = None
        h.cal_process = None
        assert h.start_fmcw_radar(dict(params))["status"] == "ok"
        for a in angles:
            resp = h.isar_capture({"angle_deg": a, **view})
            assert resp["status"] == "ok", resp
        found = h.isar_find_distance({})
        assert found["status"] == "ok", found
        return {**resp["data"], "found_r0": found["data"]["r0"]}
    finally:
        phaser_sim.SimSDR.NOISE_SIGMA = original


@pytest.fixture(scope="module")
def js_isar(tmp_path_factory):
    if NODE is None:
        pytest.skip("node is not installed")
    sim_dir = (ROOT / "frontend-radar" / "src" / "sim").as_posix()
    driver = tmp_path_factory.mktemp("radar_isar_parity") / "driver.mjs"
    driver.write_text(_JS_ISAR_DRIVER % {"sim": sim_dir})
    proc = subprocess.run([NODE, str(driver), json.dumps([list(c) for c in ISAR_CASES])],
                          capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise AssertionError(f"node driver failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


@pytest.mark.parametrize("name,params,angles,view", ISAR_CASES, ids=[c[0] for c in ISAR_CASES])
def test_isar_image_matches_python(name, params, angles, view, js_isar):
    py, js = _python_isar(params, angles, view), js_isar[name]
    for key in ("n_steps", "angles", "r0", "extent", "clutter", "flip"):
        assert js[key] == py[key], key
    assert js["found_r0"] == pytest.approx(py["found_r0"], abs=1e-3)
    np.testing.assert_allclose(js["x_axis"], py["x_axis"], atol=AXIS_TOL)
    a, b = np.asarray(py["image_db"]), np.asarray(js["image_db"])
    assert a.shape == b.shape
    keep = (a > ISAR_FLOOR_DB) | (b > ISAR_FLOOR_DB)
    np.testing.assert_allclose(b[keep], a[keep], atol=0.1)
