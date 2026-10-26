"""Calibration recovers from the one failure it can diagnose itself.

On kit phaser-00 the contrast gate aborted with 0.93 dB because the shipped
config.Rx_freq put the receiver 20 MHz past an IF ceiling that varies ~200 MHz
between kits. The console said "no signal is reaching the array" and listed
the HB100, the stored frequency and the cables -- none of which was wrong.

A unit that has never been characterized and fails that gate is worth
recovering rather than reporting, because the bench conditions a sweep needs
(HB100 powered and aimed at the array) are exactly the ones calibration needs,
so anyone who got that far has already set them up.

The safety properties are what these tests are mostly about: a sweep that
hears nothing must save nothing, and a unit that already has a measurement
must never be re-swept over.
"""

import sys
import types
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import phaser_functions as pf


@pytest.fixture(scope="module")
def cal():
    """phaser_cal_headless imports pyadi at module level; stub it."""
    adi = sys.modules.setdefault("adi", types.ModuleType("adi"))
    if not hasattr(adi, "ad9361"):
        adi.ad9361 = type("ad9361", (), {})
    cn0566 = sys.modules.setdefault("adi.cn0566", types.ModuleType("adi.cn0566"))
    if not hasattr(cn0566, "CN0566"):
        cn0566.CN0566 = type("CN0566", (), {})
    adi.cn0566 = cn0566
    import phaser_cal_headless
    return phaser_cal_headless


@pytest.fixture
def fast(monkeypatch):
    """The sweep settles 0.2 s per step; 57 steps is not a unit test."""
    monkeypatch.setattr(pf.time, "sleep", lambda _s: None)


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(pf, "REPO_DIR", str(tmp_path))
    return tmp_path


HB100 = 10.40763e9


class FakeSDR:
    """A receiver that only hears while its rx_lo is inside a window.

    This is the actual hardware behavior the recovery exists for: reception
    is a property of the IF, so retuning the Pluto is what makes the array
    hear or not.
    """

    sample_rate = 30_720_000

    def __init__(self, window_hz, n=1024, seed=3):
        self.window = window_hz          # None = hears nothing anywhere
        self.rx_lo = int(2.2e9)
        self.n = n
        self.rng = np.random.default_rng(seed)
        self.destroyed = 0

    def rx_destroy_buffer(self):
        self.destroyed += 1

    def rx(self):
        inside = (self.window is not None
                  and self.window[0] <= self.rx_lo <= self.window[1])
        t = np.arange(self.n) / self.sample_rate
        sig = (1000.0 if inside else 0.0) * np.exp(2j * np.pi * 1e6 * t)
        noise = (self.rng.normal(0, 1.0, self.n)
                 + 1j * self.rng.normal(0, 1.0, self.n))
        out = sig + noise
        return np.array([out, out])


class FakePhaser:
    phase_step_size = 2.8125

    def __init__(self, sdr):
        self.sdr = sdr
        self.frequency = 0
        self.SignalFreq = HB100
        self.Averages = 4
        self.gains = [0] * 8

    def set_chan_gain(self, chan, gain, apply_cal=False):
        self.gains[chan] = gain

    def set_chan_phase(self, chan, phase, apply_cal=False):
        pass


# --- the policy -------------------------------------------------------------

def test_a_healthy_contrast_never_triggers_a_sweep(cal, store):
    assert cal.should_characterize(33.0, False, True) is False


def test_a_failed_gate_on_a_fresh_unit_triggers_a_sweep(cal, store):
    assert cal.should_characterize(0.93, False, True) is True


def test_a_unit_that_already_has_a_measurement_is_never_re_swept(cal, store):
    """The important one. A failed gate here means a dead source or a cable,
    and sweeping under those conditions would overwrite a good measurement
    with a bad one."""
    pf.save_if_cal({"rx_freq_hz": 1.69e9, "clean_min_hz": 1.2e9,
                    "clean_max_hz": 2.175e9, "best_snr_db": 86.0})
    assert cal.should_characterize(0.93, False, True) is False


def test_it_is_attempted_at_most_once(cal, store):
    assert cal.should_characterize(0.93, True, True) is False


def test_an_unmeasured_hb100_frequency_blocks_the_sweep(cal, store):
    """The state a brand-new kit is in, and the one that would mislead.

    The sweep tracks the LO against the stored source frequency to keep the
    tone inside the analysis window. Against config.py's 10.525 GHz nominal
    -- on a source the labs allow to sit anywhere in 10.1-10.7 GHz -- it
    tracks the wrong frequency and hears nothing at EVERY IF, which reads as
    "the IF was not the problem" when the IF was never tested. Run the HB100
    search first; there is nothing to sweep against until then.
    """
    assert cal.should_characterize(0.93, False, False) is False


# --- the recovery -----------------------------------------------------------

def test_it_finds_the_window_and_leaves_the_radio_inside_it(cal, store, fast):
    sdr = FakeSDR(window_hz=(1.2e9, 2.175e9))
    phaser = FakePhaser(sdr)

    new_if = cal.characterize_in_place(phaser, sdr, HB100)

    assert new_if is not None
    assert 1.2e9 < new_if < 2.175e9, f"chose {new_if/1e9:.3f} GHz, outside the window"
    # Tuned there, both halves together -- the LO has to track the IF or the
    # tone leaves the analysis window and every later capture reads as deaf.
    assert sdr.rx_lo == int(new_if)
    assert phaser.frequency == int((HB100 + new_if) / 4.0)
    assert phaser.gains == [127] * 8, "array left ready to calibrate"


def test_what_it_finds_is_what_it_stores(cal, store, fast):
    sdr = FakeSDR(window_hz=(1.2e9, 2.175e9))
    new_if = cal.characterize_in_place(FakePhaser(sdr), sdr, HB100)
    stored = pf.load_if_cal()
    assert stored is not None
    assert stored["rx_freq_hz"] == pytest.approx(new_if)
    assert stored["hb100_freq_hz"] == pytest.approx(HB100)


def test_a_bench_that_hears_nothing_saves_nothing(cal, store, fast):
    """The safety property. An operator who runs this with the HB100 off
    must not end up with a stored window invented out of noise -- that would
    turn a recoverable mistake into a kit that is miscalibrated for good."""
    sdr = FakeSDR(window_hz=None)
    assert cal.characterize_in_place(FakePhaser(sdr), sdr, HB100) is None
    assert pf.load_if_cal() is None


def test_a_failed_recovery_leaves_an_existing_measurement_alone(cal, store, fast):
    pf.save_if_cal({"rx_freq_hz": 1.69e9, "clean_min_hz": 1.2e9,
                    "clean_max_hz": 2.175e9, "best_snr_db": 86.0})
    sdr = FakeSDR(window_hz=None)
    assert cal.characterize_in_place(FakePhaser(sdr), sdr, HB100) is None
    assert pf.load_if_cal()["rx_freq_hz"] == pytest.approx(1.69e9)


def test_the_sweep_flushes_between_tunings(cal, store, fast):
    """Without this every step reads the previous step's tuning, which is
    what put the HB100 search 8 MHz off a real tone (PR #29)."""
    sdr = FakeSDR(window_hz=(1.2e9, 2.175e9))
    cal.characterize_in_place(FakePhaser(sdr), sdr, HB100)
    steps = int((pf.IF_SWEEP_STOP_HZ - pf.IF_SWEEP_START_HZ) / pf.IF_SWEEP_STEP_HZ)
    assert sdr.destroyed >= steps


def test_a_narrow_window_is_refused_rather_than_centered(cal, store, fast):
    """One or two clean steps is measurement noise, not a window."""
    sdr = FakeSDR(window_hz=(1.60e9, 1.625e9))
    assert cal.characterize_in_place(FakePhaser(sdr), sdr, HB100) is None
    assert pf.load_if_cal() is None
