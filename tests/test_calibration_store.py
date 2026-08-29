"""The JSON calibration store, and its fallback to the legacy pickle/txt files.

Background: a JSON store existed at 16f359c and the GUI-era phaser_cal.py used
it, but the headless rewrite went back to pyadi-iio's pickle-writing CN0566
methods and orphaned it, so b66125a deleted the then-unused JSON code. The
readers had always been pickle and were never switched. This is the migration
finished properly -- which means the fallback matters as much as the store,
because Pis in the field are calibrated in the old format right now.
"""

import json
import pickle

import pytest

import phaser_functions as pf


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Point the module at a scratch directory so tests never touch real cal."""
    monkeypatch.setattr(pf, "REPO_DIR", str(tmp_path))
    return tmp_path


def test_round_trip(isolated_store):
    pf.save_phase_cal([1, 2, 3, 4, 5, 6, 7, 8])
    assert pf.load_cal_values("phase_cal", [0.0] * 8, 8) == [1, 2, 3, 4, 5, 6, 7, 8]


def test_writes_are_merged_not_replaced(isolated_store):
    """Calibrations are run one at a time; writing phase must not drop gain."""
    pf.save_gain_cal([0.5] * 8)
    pf.save_phase_cal([10.0] * 8)
    pf.save_channel_cal([0.0, -3.0])
    pf.save_hb100_cal(10.425e9)

    data = pf.load_calibration()
    assert data["gain_cal"] == [0.5] * 8
    assert data["phase_cal"] == [10.0] * 8
    assert data["channel_cal"] == [0.0, -3.0]
    assert data["hb100_freq_hz"] == 10.425e9


def test_stored_file_is_readable_json(isolated_store):
    """The point of leaving pickle: a human can read and edit this."""
    pf.save_gain_cal([0.25] * 8)
    text = (isolated_store / "calibration.json").read_text()
    assert json.loads(text)["gain_cal"] == [0.25] * 8
    assert "\n" in text, "should be indented, not one line"


def test_falls_back_to_legacy_pickle(isolated_store):
    """A Pi calibrated before this change must keep working."""
    (isolated_store / "gain_cal_val.pkl").write_bytes(pickle.dumps([0.75] * 8))
    assert pf.load_cal_values("gain_cal", [1.0] * 8, 8) == [0.75] * 8


def test_json_wins_over_legacy_pickle(isolated_store):
    (isolated_store / "phase_cal_val.pkl").write_bytes(pickle.dumps([99.0] * 8))
    pf.save_phase_cal([1.0] * 8)
    assert pf.load_cal_values("phase_cal", [0.0] * 8, 8) == [1.0] * 8


def test_fallback_is_per_key(isolated_store):
    """Re-running only phase must not silently revert gain to defaults."""
    (isolated_store / "gain_cal_val.pkl").write_bytes(pickle.dumps([0.6] * 8))
    pf.save_phase_cal([5.0] * 8)
    assert pf.load_cal_values("phase_cal", [0.0] * 8, 8) == [5.0] * 8
    assert pf.load_cal_values("gain_cal", [1.0] * 8, 8) == [0.6] * 8


def test_hb100_falls_back_to_the_text_file(isolated_store):
    (isolated_store / "hb100_cal.txt").write_text("10425136718.75")
    assert pf.load_hb100_cal() == pytest.approx(10425136718.75)


def test_hb100_json_wins_and_raises_when_absent(isolated_store):
    with pytest.raises(FileNotFoundError):
        pf.load_hb100_cal()
    (isolated_store / "hb100_cal.txt").write_text("1.0")
    pf.save_hb100_cal(10.5e9)
    assert pf.load_hb100_cal() == 10.5e9


def test_corrupt_json_does_not_stop_startup(isolated_store):
    """A bad calibration file must degrade to defaults, not crash the backend."""
    (isolated_store / "calibration.json").write_text("{not json")
    assert pf.load_calibration() == {}
    assert pf.load_cal_values("gain_cal", [1.0] * 8, 8) == [1.0] * 8


def test_write_is_atomic(isolated_store, monkeypatch):
    """A crash mid-write must not truncate the file that holds every value."""
    pf.save_gain_cal([0.5] * 8)

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(pf.os, "replace", boom)
    with pytest.raises(OSError):
        pf.save_phase_cal([1.0] * 8)

    assert pf.load_calibration()["gain_cal"] == [0.5] * 8, "previous store lost"
    leftovers = list(isolated_store.glob(".calibration-*"))
    assert leftovers == [], f"temp file not cleaned up: {leftovers}"


@pytest.mark.parametrize("junk", [None, "nope", [1, 2], {"a": 1}, ["x"] * 8])
def test_garbage_never_yields_the_wrong_length(isolated_store, junk):
    """Downstream code indexes these by element; a short list is a crash."""
    assert len(pf._coerce_list(junk, [0.0] * 8, 8)) == 8
