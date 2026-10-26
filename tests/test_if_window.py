"""Characterizing a unit's usable IF, and deciding which IF to run at.

The bug these guard against: config.py ships Rx_freq = 2.2 GHz, kit "phaser"
can hear there, kit phaser-00 cannot, and nothing anywhere said so. config.py
is site-owned and deliberately never deployed over, so the hand-edit that kept
one kit working could not travel to the other. The answer is a per-unit
measurement stored where deployment can read it, and a resolver that prefers
it over any constant.
"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import phaser_functions as pf


HB100_00 = 10.407630e9


def sweep(dead_from_hz, start=1.2e9, stop=2.6e9, step=25e6,
          clean_snr=86.0, dead_snr=14.0):
    """A sweep shaped like a real one: clean up to an edge, noise above it."""
    rows = []
    f = start
    while f <= stop + 1.0:
        snr = clean_snr if f < dead_from_hz else dead_snr
        rows.append({"rx_freq_hz": f, "lo_hz": HB100_00 + f,
                     "peak_dbfs": -2.3 if snr == clean_snr else -75.0,
                     "snr_db": snr})
        f += step
    return rows


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(pf, "REPO_DIR", str(tmp_path))
    return tmp_path


def cfg(**kw):
    return types.SimpleNamespace(**kw)


# --- choosing an IF from a sweep -------------------------------------------

def test_phaser_00_shape_picks_the_middle_of_its_clean_run():
    """Measured: clean to 2.175, dead from 2.200. Middle of 1.2-2.175."""
    window = pf.choose_rx_freq(sweep(dead_from_hz=2.2e9))
    assert window["ok"]
    assert window["clean_max_hz"] == pytest.approx(2.175e9)
    assert window["rx_freq_hz"] == pytest.approx(1.69e9)


def test_the_other_kit_shape_picks_a_different_if():
    """Kit "phaser" was clean to ~2.375. Same rule, ~100 MHz higher answer.

    The point of the rule: each unit lands where its own measurement puts it,
    and neither answer is transplanted from the other.
    """
    window = pf.choose_rx_freq(sweep(dead_from_hz=2.4e9))
    assert window["ok"]
    assert window["rx_freq_hz"] == pytest.approx(1.79e9)


def test_the_middle_is_far_from_the_edge_that_varies():
    """Both kits' answers clear the LOWER of the two measured ceilings.

    The two units differ by ~200 MHz at the top edge, so the margin that
    matters is whether a unit characterized today would still be inside its
    window after drifting by about that much. 1.69 and 1.79 GHz sit 390 and
    290 MHz under phaser-00's 2.18, which covers it.
    """
    for dead_from in (2.2e9, 2.4e9):
        chosen = pf.choose_rx_freq(sweep(dead_from_hz=dead_from))["rx_freq_hz"]
        assert chosen < pf.IF_UNMEASURED_CEILING_HZ - 250e6


def test_an_isolated_clean_step_beyond_the_dead_region_is_not_the_window():
    """Contiguity. One clean reading the far side of a dead gap is an
    artefact, and averaging it in would drag the answer onto the skirt."""
    rows = sweep(dead_from_hz=2.2e9)
    for row in rows:
        if row["rx_freq_hz"] == pytest.approx(2.5e9):
            row["snr_db"] = 86.0
    window = pf.choose_rx_freq(rows)
    assert window["ok"]
    assert window["clean_max_hz"] == pytest.approx(2.175e9)
    assert window["rx_freq_hz"] == pytest.approx(1.69e9)


def test_a_dead_sweep_is_not_rejected_by_shape_alone():
    """Documents a real limit of this function, so the caller covers it.

    With nothing received, every step is within tolerance of every other, so
    the clean run is the whole sweep and the shape looks perfect. Only the
    absolute level gives it away -- which is why the entrypoint gates on
    MIN_BOOTSTRAP_SNR_DB before trusting any of this.
    """
    window = pf.choose_rx_freq(sweep(dead_from_hz=1.2e9))
    assert window["ok"]                        # shape says fine...
    assert window["best_snr_db"] == pytest.approx(14.0)   # ...level does not


def test_a_window_too_narrow_to_be_a_window_is_refused():
    rows = sweep(dead_from_hz=1.25e9)      # one clean step
    window = pf.choose_rx_freq(rows)
    assert not window["ok"]
    assert "not a window" in window["reason"]


def test_the_chosen_if_is_rounded_to_something_a_person_can_read():
    window = pf.choose_rx_freq(sweep(dead_from_hz=2.2e9))
    assert window["rx_freq_hz"] % pf.IF_ROUNDING_HZ == 0


# --- storing it -------------------------------------------------------------

def test_a_characterization_survives_a_round_trip(store):
    rows = sweep(dead_from_hz=2.2e9)
    window = pf.choose_rx_freq(rows)
    pf.save_if_cal(window, signal_freq_hz=HB100_00, rows=rows)

    loaded = pf.load_if_cal()
    assert loaded["rx_freq_hz"] == pytest.approx(1.69e9)
    assert loaded["clean_max_hz"] == pytest.approx(2.175e9)
    assert len(loaded["sweep"]) == len(rows)


def test_characterizing_does_not_disturb_the_other_calibrations(store):
    """Calibrations are run separately; writing one must not drop another."""
    pf.save_phase_cal([1.0] * 8)
    pf.save_hb100_cal(HB100_00)
    pf.save_if_cal(pf.choose_rx_freq(sweep(dead_from_hz=2.2e9)))

    data = pf.load_calibration()
    assert data["phase_cal"] == [1.0] * 8
    assert data["hb100_freq_hz"] == pytest.approx(HB100_00)
    assert data["if_window"]["rx_freq_hz"] == pytest.approx(1.69e9)


def test_a_corrupt_characterization_reads_as_absent(store):
    pf.save_calibration(if_window={"clean_min_hz": 1.0})   # no rx_freq_hz
    assert pf.load_if_cal() is None


# --- resolving which IF to run at ------------------------------------------

def test_with_no_measurement_the_config_value_is_used_but_flagged(store):
    chosen = pf.rx_freq_source(cfg(Rx_freq=2.2e9))
    assert chosen["rx_freq_hz"] == pytest.approx(2.2e9)
    assert any("not been characterized" in w for w in chosen["warnings"])


def test_the_measurement_beats_config(store):
    """The decision this whole change turns on.

    config.py is whatever the card shipped with; the measurement is this
    unit. A value that is wrong here cannot be allowed to win just because
    somebody's SD card has carried it since 2026.
    """
    pf.save_if_cal(pf.choose_rx_freq(sweep(dead_from_hz=2.2e9)))
    chosen = pf.rx_freq_source(cfg(Rx_freq=2.2e9))
    assert chosen["rx_freq_hz"] == pytest.approx(1.69e9)
    assert "measured" in chosen["source"]
    assert any("disagrees" in w for w in chosen["warnings"])


def test_agreeing_config_produces_no_noise(store):
    pf.save_if_cal(pf.choose_rx_freq(sweep(dead_from_hz=2.2e9)))
    assert pf.rx_freq_source(cfg(Rx_freq=1.69e9))["warnings"] == []


def test_an_explicit_override_beats_the_measurement_and_says_so(store):
    """A separate name, so forcing a value is a decision and not a leftover."""
    pf.save_if_cal(pf.choose_rx_freq(sweep(dead_from_hz=2.2e9)))
    chosen = pf.rx_freq_source(cfg(Rx_freq=2.2e9, Rx_freq_override=1.5e9))
    assert chosen["rx_freq_hz"] == pytest.approx(1.5e9)
    assert any("overriding" in w for w in chosen["warnings"])


def test_with_nothing_at_all_the_bootstrap_default_is_used(store):
    chosen = pf.rx_freq_source(cfg())
    assert chosen["rx_freq_hz"] == pytest.approx(pf.DEFAULT_RX_FREQ_HZ)
    assert any("bootstrap" in w for w in chosen["warnings"])


def test_the_bootstrap_default_is_inside_every_kit_measured_so_far(store):
    """It has to be: it is the IF the HB100 search runs at on a fresh kit,
    and the search is what the characterization needs in order to run."""
    assert pf.DEFAULT_RX_FREQ_HZ < pf.IF_UNMEASURED_CEILING_HZ - 400e6


def test_resolve_rx_freq_is_just_the_number(store):
    pf.save_if_cal(pf.choose_rx_freq(sweep(dead_from_hz=2.2e9)))
    assert pf.resolve_rx_freq(cfg(Rx_freq=2.2e9)) == pytest.approx(1.69e9)
