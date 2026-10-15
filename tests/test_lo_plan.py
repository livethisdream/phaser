"""Chain arithmetic, and which axis the deafness is actually on.

LO = SignalFreq + Rx_freq, ADF4159 register = LO / 4. Both halves have failed
silently on this hardware. Writing the LO undivided (c9761c8) asked for four
times the frequency; running at an IF the unit cannot pass does the same. In
neither case is there anything to notice at runtime -- the attribute write is
accepted, the readback agrees, and MUXOUT is not wired as lock detect on this
board, reading 0 even at a chain demonstrably producing a 43 dB pattern.

THE AXIS CHANGED, AND THIS FILE CHANGED WITH IT.

This file used to assert an LO ceiling, measured on kit "phaser" (HB100 at
10.446953 GHz, 2026-09-24) by stepping Rx_freq and reading the peak: clean to
an LO of 12.797 GHz, degrading by 12.847, receiving nothing by 12.897.

That experiment could not support the conclusion. With the source fixed,
LO = SignalFreq + Rx_freq moves the LO and the IF together, so "dead at LO
12.897" and "dead at IF 2.45" are the same rows.

Separated on phaser-00, 2026-10-15, by parking the Pluto's rx_lo and moving
only the CN0566 LO -- which walks the tone across baseband rather than
retuning the receiver:

    rx_lo parked   LO swept over      tone tracked   peak
      2.0 GHz      12.398-12.408 GHz    11 of 11     -2.3 to -3.1 dBFS
      2.2 GHz      12.598-12.608 GHz     0 of 11     -75 dBFS, noise only

The ADF4159 received cleanly at 3.14691 GHz and nothing at 3.14950 GHz --
2.6 MHz apart, opposite outcomes, differing only in rx_lo. The limit is on
the IF. The LO was unconstrained across 11.708-12.608 GHz, every LO a
10.4 GHz source can reach.

So the old rows are kept below, reinterpreted as IF measurements, and the
ceiling they imply is no longer a constant anything is judged against: it is
per-unit and measured (phaser_characterize_headless.py), because the two
units measured so far are ~200 MHz apart and config.py ships a value that
one of them cannot hear.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import phaser_functions as pf


HB100 = 10.446953e9          # kit "phaser"
HB100_00 = 10.407630e9       # kit "phaser-00"


@pytest.fixture
def uncharacterised(monkeypatch, tmp_path):
    """No stored measurement, so the fleet-wide fallback is what answers."""
    monkeypatch.setattr(pf, "REPO_DIR", str(tmp_path))
    return tmp_path


# --- arithmetic, unchanged and still true -----------------------------------

def test_lo_is_signal_plus_if():
    chain = pf.lo_chain(HB100, 1.9e9)
    assert chain["lo_hz"] == HB100 + 1.9e9


def test_register_is_a_quarter_of_the_lo():
    """The /4 is the CN0566's divider ahead of the PLL's RFIN, not a choice."""
    chain = pf.lo_chain(10.0e9, 2.0e9)
    assert chain["lo_hz"] == 12.0e9
    assert chain["adf4159_hz"] == 3.0e9


# --- the measurements, re-read on the IF axis -------------------------------

def test_the_ifs_that_received_cleanly_are_below_the_fleet_ceiling():
    """Kit "phaser" heard 2.15, 2.20 and 2.35; phaser-00 heard up to 2.18.

    The fleet fallback is the LOWEST clean edge anyone has measured, so only
    the IFs at or under phaser-00's 2.18 are unambiguously inside it.
    """
    assert pf.IF_UNMEASURED_CEILING_HZ == 2.18e9
    assert pf.IF_UNMEASURED_DEAD_HZ == 2.20e9


def test_an_uncharacterised_unit_is_warned_even_at_a_good_if(uncharacterised):
    """1.7 GHz works on both kits -- and is still not a measurement of THIS one.

    The whole failure being defended against is a number that was right
    somewhere else, so "inside what other kits manage" must not read as OK.
    """
    problems = pf.rf_chain_warnings(HB100_00, 1.7e9)
    assert any("never been characterised" in p for p in problems)


def test_the_shipped_config_value_warns_on_an_uncharacterised_unit(uncharacterised):
    """config.py ships 2.2 GHz. phaser-00 is deaf there and said nothing."""
    problems = pf.rf_chain_warnings(HB100_00, 2.2e9)
    assert any("receiving nothing" in p for p in problems), problems


def test_an_if_just_over_the_fleet_ceiling_is_hedged_not_condemned(uncharacterised):
    """2.19 GHz: past phaser-00's clean edge, inside kit "phaser"'s. Unknown."""
    problems = pf.rf_chain_warnings(HB100_00, 2.19e9)
    assert any("was not measured to" in p for p in problems), problems
    assert not any("receiving nothing" in p for p in problems)


# --- with a measurement, the unit's own window is what counts ---------------

def test_a_measured_window_replaces_the_fleet_fallback(uncharacterised):
    """phaser-00's real window. 2.0 GHz is fine here and warns without it."""
    window = {"clean_min_hz": 1.2e9, "clean_max_hz": 2.175e9,
              "rx_freq_hz": 1.69e9, "best_snr_db": 86.0}
    assert pf.rf_chain_warnings(HB100_00, 1.69e9, window=window) == []


def test_a_measured_window_condemns_what_it_actually_excludes(uncharacterised):
    window = {"clean_min_hz": 1.2e9, "clean_max_hz": 2.175e9,
              "rx_freq_hz": 1.69e9, "best_snr_db": 86.0}
    problems = pf.rf_chain_warnings(HB100_00, 2.2e9, window=window)
    assert any("outside this unit's measured window" in p for p in problems)


def test_sitting_near_a_measured_edge_is_called_out(uncharacterised):
    """Inside, but only just. The edge is where unit variation lives."""
    window = {"clean_min_hz": 1.2e9, "clean_max_hz": 2.175e9,
              "rx_freq_hz": 1.69e9, "best_snr_db": 86.0}
    problems = pf.rf_chain_warnings(HB100_00, 2.15e9, window=window)
    assert any("from the edge" in p for p in problems), problems


def test_a_window_measured_on_one_kit_does_not_judge_another():
    """The regression this whole change exists to prevent.

    Kit "phaser" is clean at 2.2; phaser-00 is deaf there. Judged against its
    own window each is right, and against the other's each is wrong.
    """
    phaser = {"clean_min_hz": 1.2e9, "clean_max_hz": 2.375e9,
              "rx_freq_hz": 1.79e9, "best_snr_db": 64.0}
    phaser_00 = {"clean_min_hz": 1.2e9, "clean_max_hz": 2.175e9,
                 "rx_freq_hz": 1.69e9, "best_snr_db": 86.0}
    assert pf.rf_chain_warnings(HB100, 2.2e9, window=phaser) == []
    assert any("outside" in p
               for p in pf.rf_chain_warnings(HB100_00, 2.2e9, window=phaser_00))
