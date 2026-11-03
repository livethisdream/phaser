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


def _python_frame(mode, params):
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
        h.cw_lock = threading.Lock()
        h.fmcw_params = {}
        h.fmcw_saved = {}
        h.fmcw_hw = None
        h.fmcw_cfg = None
        h.fmcw_plan = None
        h.cal_process = None
        if mode == "cw":
            assert h.start_cw_radar(dict(params))["status"] == "ok"
            return h.do_cw_radar_frame()
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
        if key == "map":
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
]

_JS_SAVE_DRIVER = """
import { createRadarEngine } from '%(sim)s/engine.js';
const cases = JSON.parse(process.argv[2]);
const out = {};
for (const [name, params, iqCh] of cases) {
    const engine = createRadarEngine({ noiseSigma: 0 });
    engine.handle('start_fmcw_radar', params);
    const resp = engine.handle('save_fmcw_capture', { iq_ch: iqCh });
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
        h.cw_params, h.cw_saved_sdr, h.cw_lock = {}, {}, threading.Lock()
        h.fmcw_params, h.fmcw_saved, h.fmcw_hw = {}, {}, None
        h.fmcw_cfg = h.fmcw_plan = h.cal_process = None
        assert h.start_fmcw_radar(dict(params))["status"] == "ok"
        resp = h.save_fmcw_capture({"iq_ch": iq_ch})
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
        if value.dtype.kind == "c":
            scale = float(np.max(np.abs(value)))
            worst = float(np.max(np.abs(value - other)))
            assert worst <= 1e-4 * scale, f"{name}.{key}: samples off by {worst:g} of {scale:g}"
        else:
            assert float(other) == pytest.approx(float(value), rel=1e-12), f"{name}.{key}"
