"""The JavaScript simulator must agree with the Python one.

frontend/src/sim/ is a hand port of phaser_sim.py and
PhaserHeadless.do_sweep(). Two implementations of the same physics drift, and
when they do the GitHub Pages demo quietly stops teaching what the hardware
does. This is the test that makes drift fail the build.

Constants are handled separately and more strongly: tools/gen_sim_constants.py
generates them from the Python, and CI fails if the committed output is stale.
So what is left for this file is *behavioural* drift -- an algorithm change on
one side and not the other.

--- Why noise is off ------------------------------------------------------

NumPy's PCG64 stream cannot be reproduced in JS, so a noisy comparison could
only ever be statistical. Both sides run with NOISE_SIGMA = 0 and the
deterministic physics is compared sample for sample instead.

--- Why gains are compared only above a floor -----------------------------

phaser_sim synthesizes in complex64; the JS uses float64 throughout. Wherever
the array forms a perfect null the two produce different numerical garbage --
values around -330 dBFS, some 300 dB below anything a real receiver could
show, and clamped by do_sweep()'s own 1e-15 guard. Comparing those compares
rounding noise. Above the floor the two agree to ~1e-4 dB, which is eight
orders tighter than any drift worth catching.

--- The monopulse phase is now compared directly ---------------------------

This comparison used to have to wrap into (-pi, pi] first. do_sweep() computed
`np.angle(sum) - np.angle(delta)`, which spans (-2pi, 2pi), so the same
physical angle came out as +pi/2 here and -3pi/2 there depending on which side
of the branch cut the two angles landed -- and that was decided by float32 vs
float64 rounding, i.e. by nothing physical.

That was a real defect in do_sweep(), found by this test: a difference of
exactly 2*pi between two implementations means both computed the same angle and
then labelled it differently. It is fixed now (beam_phase is the angle of
sum * conj(delta)), so PhaseDiff and ErrorFunc are compared raw, with no
wrapping. They agree to ~5e-14 and ~1e-15 respectively.

Keeping the comparison unwrapped is deliberate: it is what would catch a
regression of that fix.
"""

import json
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

np = pytest.importorskip("numpy")

NODE = shutil.which("node")

# Sweep points below this are numerical noise in both languages (see docstring).
MEANINGFUL_DBFS = -100.0

# Monopulse phase tolerance, radians. See the note at its use site.
PHASE_TOL = 1e-3

# Cases are (name, set_state patch, set_sweep_params kwargs). Between them they
# exercise every knob the sweep branches on. A new physics knob belongs here --
# parity is only as strong as this matrix.
CASES = [
    ("boresight", {}, {}),
    ("steered", {"ignore_res": False, "steer_res": 1.0}, {"steer_min": -60, "steer_max": 60}),
    ("tapered", {"gainList": [6, 27, 66, 100, 100, 66, 27, 6]}, {}),
    ("three_bit", {"bits": 3}, {}),
    ("beam_squint", {"BW": 10.0}, {}),
    ("phase_offsets", {"phaseList": [0, 15, 30, 45, 60, 75, 90, 105]}, {}),
    ("digital_weights", {"B0_Gain": 0.7, "B1_Gain": 1.0, "Beam0_Phase": 35.0}, {}),
    (
        "mvdr_interferer",
        {
            "bfMode": "mvdr",
            "sim_interferer_enable": True,
            "sim_interferer_angle_deg": 30.0,
            "sim_interferer_power_db": 0.0,
        },
        {},
    ),
    (
        "interferer_manual",
        {
            "sim_interferer_enable": True,
            "sim_interferer_angle_deg": -45.0,
            "sim_interferer_power_db": 6.0,
        },
        {},
    ),
    ("averaged", {"Averages": 3}, {}),
]

CASE_IDS = [c[0] for c in CASES]


# --- the two implementations ----------------------------------------------

def _python_sweep(patch, sweep_params, phase_error=None):
    """Run do_sweep() against the Python simulator with noise disabled."""
    if "adi" not in sys.modules:
        # pyadi-iio binds libiio at import; sim mode never touches it.
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
        if phase_error is not None:
            h.array.element_phase_error = list(phase_error)
        if patch:
            h.handle_command({"cmd": "set_state", "data": {"state": patch}})
        if sweep_params:
            h.handle_command({"cmd": "set_sweep_params", "data": sweep_params})
        return h.do_sweep()
    finally:
        phaser_sim.SimSDR.NOISE_SIGMA = original


_JS_DRIVER = """
import { createEngine } from '%(sim)s/engine.js';
const cfg = JSON.parse(process.argv[2]);
const engine = createEngine({ noiseSigma: 0 });
if (cfg.phaseError) engine._internal.array.elementPhaseError = cfg.phaseError;
if (cfg.patch && Object.keys(cfg.patch).length) engine.setState(cfg.patch);
if (cfg.sweepParams && Object.keys(cfg.sweepParams).length) {
    engine.setSweepParams(cfg.sweepParams);
}
process.stdout.write(JSON.stringify(engine.doSweep()));
"""


def _js_sweep(patch, sweep_params, phase_error=None, tmp_path=None):
    """Run the same sweep through frontend/src/sim/engine.js via node."""
    sim_dir = (ROOT / "frontend" / "src" / "sim").as_posix()
    driver = tmp_path / "driver.mjs"
    driver.write_text(_JS_DRIVER % {"sim": sim_dir})

    cfg = json.dumps({
        "patch": patch,
        "sweepParams": sweep_params,
        "phaseError": phase_error,
    })
    proc = subprocess.run(
        [NODE, str(driver), cfg],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        raise AssertionError(f"node driver failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


# --- tests -----------------------------------------------------------------

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


@pytest.fixture(scope="module")
def _importable():
    try:
        _python_sweep({}, {})
    except Exception as exc:  # noqa: BLE001 - zmq/msgpack absent
        pytest.skip(f"phaser_headless unavailable: {exc}")


@pytest.mark.parametrize("name,patch,sweep_params", CASES, ids=CASE_IDS)
def test_sweep_matches_python(name, patch, sweep_params, tmp_path, _importable):
    py = _python_sweep(patch, sweep_params)
    js = _js_sweep(patch, sweep_params, tmp_path=tmp_path)

    assert len(js["ArrayGain"]) == len(py["ArrayGain"]), (
        f"{name}: sweep produced {len(js['ArrayGain'])} points, Python produced "
        f"{len(py['ArrayGain'])}"
    )

    py_angle = np.asarray(py["ArrayAngle"])
    js_angle = np.asarray(js["ArrayAngle"])
    # The angle axis is pure steering math, so this is where a drift in
    # STEER_PI, element spacing or the squint frequency would surface first.
    assert np.max(np.abs(py_angle - js_angle)) < 1e-9, f"{name}: steering angles diverged"

    py_gain = np.asarray(py["ArrayGain"])
    js_gain = np.asarray(js["ArrayGain"])
    keep = py_gain > MEANINGFUL_DBFS
    # At least one point must survive the floor, or the comparison below is
    # vacuous. A coarse `bits` setting legitimately leaves very few: 3 bits
    # gives a 12-point sweep whose off-peak points are perfect nulls in a
    # noiseless array. The length, angle-axis, argmax and peak_signal
    # assertions carry that case.
    assert keep.sum() >= 1, f"{name}: no sweep point rose above the numerical floor"
    assert np.max(np.abs(py_gain[keep] - js_gain[keep])) < 1e-3, (
        f"{name}: array gain diverged"
    )

    py_delta = np.asarray(py["ArrayDelta"])
    js_delta = np.asarray(js["ArrayDelta"])
    dkeep = py_delta > MEANINGFUL_DBFS
    if dkeep.any():
        assert np.max(np.abs(py_delta[dkeep] - js_delta[dkeep])) < 1e-3, (
            f"{name}: delta beam diverged"
        )

    # Raw, unwrapped: a reintroduced branch cut shows up as a 2*pi difference.
    py_phase = np.asarray(py["PhaseDiff"])
    js_phase = np.asarray(js["PhaseDiff"])
    pkeep = keep & (py_delta > MEANINGFUL_DBFS)
    if pkeep.any():
        # PHASE_TOL sits three orders below a 2*pi branch jump (~6.28) and two
        # above the complex64-vs-float64 noise floor, which reaches ~1e-5 in a
        # tapered array's weak sidelobes. Tight enough to catch a regression of
        # the branch-cut fix, loose enough not to fail on precision.
        assert np.max(np.abs(py_phase[pkeep] - js_phase[pkeep])) < PHASE_TOL, (
            f"{name}: monopulse phase diverged"
        )
        assert np.max(np.abs(np.asarray(py["ErrorFunc"])[pkeep]
                             - np.asarray(js["ErrorFunc"])[pkeep])) < 1e-3, (
            f"{name}: monopulse error function diverged"
        )

    assert np.all(np.abs(js_phase) <= np.pi + 1e-9), (
        f"{name}: JS beam_phase left (-pi, pi]; it must be the angle of "
        "sum * conj(delta), not a difference of two angles"
    )

    assert abs(py["peak_signal"] - js["peak_signal"]) < 1e-3, f"{name}: peak signal diverged"
    assert int(np.argmax(py_gain)) == int(np.argmax(js_gain)), (
        f"{name}: the beam peaks at a different angle"
    )


def test_monopulse_phase_matches_without_wrapping(tmp_path, _importable):
    """No modulo. See the note in the module docstring.

    Before the branch-cut fix this comparison could only be made after folding
    both sides into (-pi, pi]; raw, they differed by exactly 2*pi at 43 of 151
    points purely because Python synthesizes in complex64 and the JS in
    float64.
    """
    py = _python_sweep({}, {})
    js = _js_sweep({}, {}, tmp_path=tmp_path)

    ok = (np.asarray(py["ArrayGain"]) > MEANINGFUL_DBFS) & (
        np.asarray(py["ArrayDelta"]) > MEANINGFUL_DBFS
    )
    diff = np.asarray(py["PhaseDiff"])[ok] - np.asarray(js["PhaseDiff"])[ok]
    assert np.max(np.abs(diff)) < PHASE_TOL, "monopulse phase diverged"

    for impl, phase in (("Python", py["PhaseDiff"]), ("JS", js["PhaseDiff"])):
        assert np.all(np.abs(np.asarray(phase)) <= np.pi + 1e-9), (
            f"{impl} beam_phase left (-pi, pi]"
        )


def test_fft_tone_matches(tmp_path, _importable):
    """The FFT trace is compared at the tone, not across the whole span.

    With noise off, every bin away from the tone is cancellation residue -- the
    Python floor sits near -260 dBFS and the JS one near -366, and neither
    number means anything. What the plot actually shows is the peak, so that is
    what gets asserted.
    """
    py = _python_sweep({}, {})
    js = _js_sweep({}, {}, tmp_path=tmp_path)

    py_gain = np.asarray(py["max_gain"])
    js_gain = np.asarray(js["max_gain"])
    assert py_gain.shape == js_gain.shape

    py_peak = int(np.argmax(py_gain))
    js_peak = int(np.argmax(js_gain))
    assert py_peak == js_peak, "FFT peak landed in a different bin"
    assert abs(py["xf"][py_peak] - js["xf"][js_peak]) < 1e-3, "FFT peak frequency diverged"
    assert abs(py_gain[py_peak] - js_gain[js_peak]) < 1e-3, "FFT peak level diverged"

    # The whole main lobe, not just its apex: catches a window or normalization
    # change that leaves the peak intact.
    lobe = slice(py_peak - 12, py_peak + 13)
    assert np.max(np.abs(py_gain[lobe] - js_gain[lobe])) < 1e-2, "FFT main lobe diverged"


def test_element_phase_error_is_modelled_identically(tmp_path, _importable):
    """The intrinsic per-element error the phase calibration exists to cancel.

    Nothing else in the matrix perturbs it, and it enters the physics at a
    different point from the commanded phase, so it needs its own case.
    """
    err = [0.0, 12.0, -7.5, 30.0, -18.0, 3.0, 22.0, -11.0]
    py = _python_sweep({}, {}, phase_error=err)
    js = _js_sweep({}, {}, phase_error=err, tmp_path=tmp_path)

    py_gain = np.asarray(py["ArrayGain"])
    js_gain = np.asarray(js["ArrayGain"])
    keep = py_gain > MEANINGFUL_DBFS
    assert np.max(np.abs(py_gain[keep] - js_gain[keep])) < 1e-3

    # And it must actually degrade the pattern, or the test proves nothing.
    clean = np.asarray(_python_sweep({}, {})["ArrayGain"])
    assert py_gain.max() < clean.max() - 0.1, (
        "element phase error did not degrade the beam; the case is not exercising it"
    )


# Ramp quantization is exercised directly rather than through a sweep. In the
# default `ignore_res` path every PhaseValue is an exact multiple of
# phase_step, so `i * phDelta / phase_step` is always an integer and the
# rounding mode never matters -- a sweep-level test cannot tell Python's
# banker's rounding from JS's round-half-up. phase_step is settable to any
# float via set_sweep_params, though, so the tie case is reachable, and getting
# it wrong puts elements a full phase LSB off.
QUANTIZATION_CASES = [
    # (phDelta, phase_step) -- the first two put every odd element on a tie.
    (1.0, 2.0),
    (2.8125, 5.625),
    (45.0, 22.5),
    (33.0, 7.0),
    (-1.0, 2.0),
    (-17.5, 5.0),
]


@pytest.mark.parametrize("ph_delta,step", QUANTIZATION_CASES)
def test_phase_ramp_quantization_matches_python(ph_delta, step, tmp_path, _importable):
    import phaser_sim
    from ADAR_pyadi_functions import ADAR_set_Phase

    offsets = [0.0, 5.0, -12.0, 3.5, 0.0, 90.0, -45.0, 17.25]

    array = phaser_sim.make_stub_array()
    ADAR_set_Phase(array, ph_delta, step, list(offsets))
    expected = [e.latched_phase for e in array.elements.values()]

    driver = tmp_path / "ramp.mjs"
    sim_dir = (ROOT / "frontend" / "src" / "sim").as_posix()
    driver.write_text(f"""
import {{ createStubArray, adarSetPhase }} from '{sim_dir}/array.js';
const a = createStubArray();
adarSetPhase(a, {ph_delta!r}, {step!r}, {json.dumps(offsets)});
process.stdout.write(JSON.stringify(a.elements.map((e) => e.latchedPhase)));
""")
    proc = subprocess.run([NODE, str(driver)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)

    assert got == pytest.approx(expected, abs=1e-9), (
        f"phase ramp diverged at phDelta={ph_delta}, step={step}. "
        "Python's round() is banker's rounding; JS Math.round breaks ties "
        "upward. array.js must use pyRound()."
    )


def test_taper_scaling_matches_python(tmp_path, _importable):
    """0-100 taper -> 0-127 register, including the zero-element attenuator."""
    import phaser_sim
    from ADAR_pyadi_functions import ADAR_set_Taper

    taper = [0, 13, 27, 50, 66, 89, 100, 7]

    array = phaser_sim.make_stub_array()
    ADAR_set_Taper(array, list(taper))
    expected = [e.latched_gain for e in array.elements.values()]

    driver = tmp_path / "taper.mjs"
    sim_dir = (ROOT / "frontend" / "src" / "sim").as_posix()
    driver.write_text(f"""
import {{ createStubArray, adarSetTaper }} from '{sim_dir}/array.js';
const a = createStubArray();
adarSetTaper(a, {json.dumps(taper)});
process.stdout.write(JSON.stringify(a.elements.map((e) => e.latchedGain)));
""")
    proc = subprocess.run([NODE, str(driver)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == expected
    assert expected[0] == 0, "a zeroed element must route through the attenuator"


def test_unlatched_writes_flatten_the_pattern_in_js(tmp_path, _importable):
    """The latch model has to survive the port.

    An ADAR write that is never latched leaves the beam pointing wherever it was
    -- the flat-line-instead-of-a-beam-pattern failure that
    tests/test_beam_pattern.py guards on the Python side. If the JS port dropped
    the shadow/latch split, the sim would stop being able to reproduce it.
    """
    driver = tmp_path / "latch.mjs"
    sim_dir = (ROOT / "frontend" / "src" / "sim").as_posix()
    driver.write_text(f"""
import {{ createStubArray, adarSetPhase }} from '{sim_dir}/array.js';
const array = createStubArray();
adarSetPhase(array, 45, 2.8125, new Array(8).fill(0));
const latched = array.elements.map((e) => e.latchedPhase);
const shadow = array.elements.map((e) => e.rxPhase);
process.stdout.write(JSON.stringify({{
    latchCount: array.latchCount, latched, shadow,
}}));
""")
    proc = subprocess.run([NODE, str(driver)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)

    assert out["latchCount"] == 1, "steering phases were never latched"
    assert out["latched"] == out["shadow"], "latched beam state does not match the shadow"
    assert len(set(out["latched"])) > 1, "phases did not form a steering ramp"
