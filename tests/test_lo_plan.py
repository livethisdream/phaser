"""LO arithmetic: LO = SignalFreq + Rx_freq, ADF4159 register = LO / 4.

Both halves of that have already failed silently on this hardware. Writing the
LO undivided (c9761c8) asked for four times the frequency; asking for an LO the
chain cannot reach does the same thing. Neither is detectable at runtime -- the
attribute write is accepted, the readback agrees, and MUXOUT is not wired as
lock detect on this board, reading 0 even at an LO demonstrably producing a
43 dB pattern. So the check is arithmetic, and these tests are what stands
behind it.

The ceiling below is measured, not from a datasheet: kit "phaser", HB100 at
10.446953 GHz, 2026-09-24, stepping Rx_freq and reading the peak out of a
30 MSPS capture. It held clean to an LO of 12.797 GHz, was visibly degrading by
12.847, and was receiving nothing by 12.897. One kit, one source -- hence a
warning rather than a refusal.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import phaser_functions as pf


HB100 = 10.446953e9


def test_lo_is_signal_plus_if():
    chain = pf.lo_chain(HB100, 1.9e9)
    assert chain["lo_hz"] == HB100 + 1.9e9


def test_register_is_a_quarter_of_the_lo():
    """The /4 is the CN0566's divider ahead of the PLL's RFIN, not a choice."""
    chain = pf.lo_chain(10.0e9, 2.0e9)
    assert chain["lo_hz"] == 12.0e9
    assert chain["adf4159_hz"] == 3.0e9


def test_the_configuration_this_kit_runs_is_clean():
    """Rx_freq 1.9 on a 10.447 GHz source: LO 12.347, measured at 59 dB SNR."""
    assert pf.lo_warnings(HB100, 1.9e9) == []


def test_the_measured_clean_rows_produce_no_lo_warning():
    """Every row that received cleanly must pass the reachability check."""
    for rx_freq in (2.15e9, 2.20e9, 2.35e9):
        problems = [p for p in pf.lo_warnings(HB100, rx_freq) if p.startswith("LO ")]
        assert problems == [], (rx_freq, problems)


def test_the_measured_dead_rows_are_refused():
    """12.897 and 12.947 GHz received nothing; both must warn about the LO."""
    for rx_freq in (2.45e9, 2.50e9):
        problems = pf.lo_warnings(HB100, rx_freq)
        assert any("receiving nothing" in p for p in problems), rx_freq


def test_between_the_last_clean_and_the_first_dead_is_hedged():
    """12.847 degraded but still received: say it is unmeasured, not broken."""
    problems = pf.lo_warnings(HB100, 2.40e9)
    assert any("was not measured to" in p for p in problems)
    assert not any("receiving nothing" in p for p in problems)


def test_an_if_that_cannot_cover_the_hb100_range_warns():
    """The one that bites a workshop rather than a bench.

    Rx_freq 2.2 works on a 10.447 GHz source -- measured, 64 dB SNR -- but the
    labs put an HB100 anywhere in 10.1-10.7 GHz, and 10.7 + 2.2 is past the
    ceiling. The kit passes its own test and goes deaf when the source is
    swapped, with no configuration change to blame.
    """
    problems = pf.lo_warnings(HB100, 2.2e9)
    assert any("cannot cover the whole HB100 range" in p for p in problems)
    # ...and it is specifically NOT complaining about this kit's own LO.
    assert not any("receiving nothing" in p for p in problems)


def test_the_shipped_if_covers_the_whole_hb100_range():
    """1.9 GHz reaches 10.9 GHz of HB100, past the 10.7 the labs allow for."""
    assert pf.lo_warnings(HB100, 1.9e9) == []
    assert pf.HB100_MAX_HZ + 1.9e9 <= pf.LO_USABLE_CEILING_HZ


def test_coverage_warning_names_the_highest_reachable_source():
    problems = pf.lo_warnings(HB100, 2.2e9)
    coverage = [p for p in problems if "cannot cover" in p][0]
    # 12.80 - 2.20 = 10.60 GHz
    assert "10.600" in coverage


def test_thresholds_are_overridable_for_a_differently_measured_kit():
    """The ceiling is one kit's measurement, so callers can supply their own."""
    assert pf.lo_warnings(HB100, 2.2e9, ceiling_hz=13.0e9, dead_hz=13.2e9) == []


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("%s: ok" % name)
    print("\nall lo-plan tests passed")
