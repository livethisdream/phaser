"""ISAR in the simulator (docs/sar-plan.md, S1).

The turntable scene puts corner reflectors on a platter 1.5 m out; each
isar_capture turns it to the given angle, records a burst and backprojects
every step so far onto the platter. These check the image, not just that it
runs: points land where they are, and two 3 cm apart come out as two.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

np = pytest.importorskip("numpy")

import phaser_radar_dsp as dsp  # noqa: E402
from test_fmcw_radar_headless import _headless  # noqa: E402


@pytest.fixture
def platter(monkeypatch):
    """Set the turntable's corners for one test: [(x, y, rcs)]."""
    import phaser_sim

    def put(points):
        monkeypatch.setattr(phaser_sim, "SCENE_TURNTABLE_POINTS", tuple(points))
    return put


def _sweep(h, angles, **view):
    resp = None
    for a in angles:
        resp = h.isar_capture({"angle_deg": a, **view})
        assert resp["status"] == "ok", resp
    return resp["data"]


def _image(data):
    return np.asarray(data["image_db"]), np.asarray(data["x_axis"]), np.asarray(data["y_axis"])


def _start(h, **params):
    resp = h.start_fmcw_radar({"scene": "turntable", "scene_noise": "clean", **params})
    assert resp["status"] == "ok", resp


def test_one_corner_off_axis_focuses_where_it_is(platter):
    platter([(0.04, 0.03, 0.1)])
    h = _headless()
    _start(h)
    data = _sweep(h, range(-30, 31, 5))
    img, x, y = _image(data)
    iy, ix = np.unravel_index(np.argmax(img), img.shape)
    assert x[ix] == pytest.approx(0.04, abs=0.006)
    # Down-range, 60 deg of rotation is coarser than across: ~1 cm, not ~2 mm.
    assert y[iy] == pytest.approx(0.03, abs=0.015)
    assert data["n_steps"] == 13


def test_two_corners_3_cm_apart_resolve_with_60_deg(platter):
    """The S1 done-when: two spots, with a clear dip between them."""
    platter([(-0.015, 0.0, 0.1), (0.015, 0.0, 0.1)])
    h = _headless()
    _start(h)
    img, x, y = _image(_sweep(h, range(-30, 31, 5)))
    row = img[np.argmin(np.abs(y))]
    left, right = row[np.argmin(np.abs(x + 0.015))], row[np.argmin(np.abs(x - 0.015))]
    mid = row[np.argmin(np.abs(x))]
    assert min(left, right) > -3.0
    assert mid < min(left, right) - 10.0


def test_the_image_builds_step_by_step_and_clear_starts_over(platter):
    platter([(0.0, 0.0, 0.1)])
    h = _headless()
    _start(h)
    assert h.isar_capture({"angle_deg": 0})["data"]["n_steps"] == 1
    assert h.isar_capture({"angle_deg": 5})["data"]["angles"] == [0.0, 5.0]
    cleared = h.isar_clear()["data"]
    assert cleared["n_steps"] == 0 and "image_db" not in cleared


def test_a_new_plan_starts_a_new_set_of_steps(platter):
    """Profiles from two plans (here, two frequencies) don't add up."""
    platter([(0.0, 0.0, 0.1)])
    h = _headless()
    _start(h)
    _sweep(h, [0, 5, 10])
    h.set_fmcw_radar_params({"output_freq": 10.2e9})
    assert h.isar_capture({"angle_deg": 15})["data"]["n_steps"] == 1


def test_view_settings_reform_without_new_steps(platter):
    platter([(0.0, 0.0, 0.1)])
    h = _headless()
    _start(h)
    _sweep(h, range(0, 31, 5))
    data = h.isar_image({"extent": 0.3, "r0": 1.4})["data"]
    assert data["n_steps"] == 7 and data["extent"] == 0.3 and data["r0"] == 1.4
    assert data["x_axis"][-1] == pytest.approx(0.15)
    # The view sticks for later steps.
    assert h.isar_capture({"angle_deg": 35})["data"]["extent"] == 0.3


def test_clutter_removal_drops_what_does_not_turn(platter):
    """Subtracting the mean profile removes still clutter -- and with it a
    corner on the axis, which doesn't move either. Hence off by default."""
    platter([(0.0, 0.0, 0.1), (0.05, 0.0, 0.1)])
    h = _headless()
    _start(h)
    angles = range(-30, 31, 5)
    img, x, y = _image(_sweep(h, angles))
    on_axis = lambda im: im[np.argmin(np.abs(y)), np.argmin(np.abs(x))]  # noqa: E731
    assert on_axis(img) > -3.0
    img_c, _, _ = _image(h.isar_image({"clutter": True})["data"])
    assert on_axis(img_c) < -10.0


@pytest.mark.parametrize("params,needle", [
    ({}, "angle_deg"),
    ({"angle_deg": "north"}, "ISAR step"),
    ({"angle_deg": 0, "r0": 50}, "Target distance"),
])
def test_bad_steps_are_refused(params, needle):
    h = _headless()
    _start(h)
    resp = h.isar_capture(params)
    assert resp["status"] == "error" and needle in resp["message"]


def test_isar_needs_fmcw_running():
    h = _headless()
    assert "Start FMCW" in h.isar_capture({"angle_deg": 0})["message"]


def test_a_single_chirp_preset_also_steps():
    """Range (Basic) is one chirp a burst: no averaging gain, but it works."""
    h = _headless()
    _start(h, preset="range")
    assert h.isar_capture({"angle_deg": 0})["data"]["n_steps"] == 1


def test_backprojection_puts_a_synthetic_point_in_place():
    """isar_backproject alone, on profiles made from the model it inverts."""
    ranges = np.linspace(0, 3, 3001)
    f_ref, r0, (px, py) = 10e9, 1.5, (-0.03, 0.02)
    profiles, angles = [], np.arange(-40, 41, 2.0)
    for a in angles:
        t = np.radians(a)
        r = np.hypot(px * np.cos(t) - py * np.sin(t), r0 + px * np.sin(t) + py * np.cos(t))
        env = np.sinc((ranges - r) / 0.3)
        profiles.append(env * np.exp(-1j * 4 * np.pi * r * f_ref / dsp.C))
    out = dsp.isar_backproject(profiles, angles, ranges, f_ref, r0)
    img, x, y = _image(out)
    iy, ix = np.unravel_index(np.argmax(img), img.shape)
    assert (x[ix], y[iy]) == (pytest.approx(px, abs=0.003), pytest.approx(py, abs=0.006))
