"""Spur rejection in the HB100 search, and the array-contrast gate on cal.

Background, from a kit debugged on 2026-09-23: the stored HB100 frequency was
10.475019531 GHz while the source was actually at 10.446953 GHz. The beamformer
analyses only +/-1.5 MHz, so a 28 MHz error left it tuned entirely off the tone,
and the loudest thing in the band became a spur the Pluto generates at a fixed
baseband offset (-20 dBFS, measured at exactly +678.000 kHz regardless of LO).

Nothing caught it. The HB100 search takes a global argmax over a stepped-LO
sweep, and because it maps each capture to absolute frequency, a fixed-baseband
spur becomes one false candidate per step -- 70 of them -- any of which can win.
Phase calibration then maximised power that had no dependence on element phase,
so it wrote noise: reruns disagreed by 256 degrees and channel_cal came out
negative, which is impossible for a magnitude ratio. The beam pattern was flat
at 8.5 dB instead of 40+ dB and the UI just showed noise.

These tests pin the two guards added in response, both driven off the one
property that separates the cases: a real tone holds its absolute frequency
when the LO moves, a spur holds its baseband offset.
"""

import numpy as np
import pytest

import phaser_functions as pf


SAMPLE_RATE = 30_000_000
NBINS = 1024
NOISE_DB = -90.0
SPUR_BASEBAND_HZ = 678_000.0   # the real one, as measured
REAL_TONE_HZ = 10_446_953_125.0


def baseband_axis(nbins=NBINS, sample_rate=SAMPLE_RATE):
    return np.fft.fftshift(np.fft.fftfreq(nbins, d=1.0 / sample_rate))


def build_scan(real_tone_hz=REAL_TONE_HZ, real_db=-30.0,
               spur_baseband_hz=SPUR_BASEBAND_HZ, spur_db=-20.0,
               f_start=10.0e9, f_stop=10.7e9, f_step=10e6, seed=0):
    """A synthetic stepped-LO scan holding a real tone and a louder spur.

    The spur is deliberately louder than the real tone, because that is the
    case that defeats a plain argmax and the whole reason these guards exist.
    """
    bb = baseband_axis()
    steps = np.arange(f_start, f_stop, f_step)
    rng = np.random.default_rng(seed)
    m = NOISE_DB + rng.normal(0.0, 0.5, size=(steps.size, bb.size))

    half_window = SAMPLE_RATE / 2.0
    for row, step in enumerate(steps):
        if spur_db is not None:
            m[row, int(np.argmin(np.abs(bb - spur_baseband_hz)))] = spur_db
        if real_tone_hz is not None:
            offset = real_tone_hz - step
            if abs(offset) <= half_window:
                m[row, int(np.argmin(np.abs(bb - offset)))] = real_db
    return m, bb, steps


# --- the template ----------------------------------------------------------

def test_fixed_baseband_spur_is_removed():
    """A signal present in the same bin at every step subtracts to nothing."""
    m, bb, steps = build_scan(real_tone_hz=None)
    residual = pf.reject_fixed_baseband_spurs(m)
    spur_col = int(np.argmin(np.abs(bb - SPUR_BASEBAND_HZ)))
    assert abs(residual[:, spur_col]).max() < 1e-9


def test_real_tone_survives_the_template():
    """A tone that moves between steps barely shifts any bin's median."""
    m, bb, steps = build_scan()
    residual = pf.reject_fixed_baseband_spurs(m)
    row = int(np.argmin(np.abs(steps - 10.450e9)))
    col = int(np.argmin(np.abs(bb - (REAL_TONE_HZ - steps[row]))))
    # Still ~60 dB clear of the floor after the subtraction.
    assert residual[row, col] > 50.0


def test_rejects_non_2d_input():
    with pytest.raises(ValueError):
        pf.reject_fixed_baseband_spurs(np.zeros(8))


# --- picking the peak ------------------------------------------------------

def test_finds_the_real_tone_not_the_louder_spur():
    m, bb, steps = build_scan()
    result = pf.find_scan_peak(m, bb, steps)
    assert result["freq_hz"] == pytest.approx(REAL_TONE_HZ, abs=30e3)


def test_reports_what_a_plain_argmax_would_have_returned():
    """The old answer is surfaced, not discarded, so the log shows the gap."""
    m, bb, steps = build_scan()
    result = pf.find_scan_peak(m, bb, steps)
    # The spur is the loudest bin, so a plain argmax lands on it -- and lands
    # far from the truth, which is the failure being guarded against.
    assert abs(result["raw_freq_hz"] - REAL_TONE_HZ) > 1e6
    assert result["spur_rejected"] is True


def test_real_tone_is_confirmed_by_several_steps():
    """With a 30 MHz window and 10 MHz steps a tone is seen ~3 times."""
    m, bb, steps = build_scan()
    result = pf.find_scan_peak(m, bb, steps)
    assert result["steps_confirming"] >= 2
    ok, reason = pf.scan_peak_is_trustworthy(result)
    assert ok, reason


def test_scan_with_only_a_spur_is_not_trusted():
    """The flat-battery / wrong-frequency bench must fail loudly."""
    m, bb, steps = build_scan(real_tone_hz=None)
    result = pf.find_scan_peak(m, bb, steps)
    ok, reason = pf.scan_peak_is_trustworthy(result)
    assert not ok
    assert "no signal" in reason or "spur" in reason


def test_spur_rejection_is_skipped_when_there_are_too_few_steps():
    """Too few rows to estimate a median: say so rather than do it badly."""
    m, bb, steps = build_scan(f_start=10.44e9, f_stop=10.47e9, f_step=10e6)
    assert steps.size < pf.SCAN_MIN_STEPS_FOR_SPUR_REJECTION
    result = pf.find_scan_peak(m, bb, steps)
    assert result["spur_rejected"] is False


def test_rejects_mismatched_shapes():
    m, bb, steps = build_scan()
    with pytest.raises(ValueError):
        pf.find_scan_peak(m, bb[:-1], steps)


def test_the_original_bench_failure_is_caught():
    """Regression: the exact 28 MHz miss that started this.

    A scan seeing only the spur must not hand back a frequency at all, because
    the frequency it would hand back is what put the beamformer 28 MHz off.
    """
    m, bb, steps = build_scan(real_tone_hz=None, spur_db=-20.0)
    result = pf.find_scan_peak(m, bb, steps)
    ok, _ = pf.scan_peak_is_trustworthy(result)
    assert not ok


# --- the contrast gate ----------------------------------------------------

class FakePhaser:
    """Records gain writes; power is scripted by the test."""

    def __init__(self):
        self.gains = [None] * 8
        self.phases = [None] * 8
        self.gain_history = []

    def set_chan_gain(self, chan, gain, apply_cal=False):
        self.gains[chan] = gain
        self.gain_history.append((chan, gain))

    def set_chan_phase(self, chan, phase, apply_cal=False):
        self.phases[chan] = phase


def scripted_capture(monkeypatch, values):
    """Make _capture_peak_dbfs return `values` in order."""
    seq = list(values)

    def fake(phaser, averages=4):
        return seq.pop(0), None

    monkeypatch.setattr(pf, "_capture_peak_dbfs", fake)


def test_contrast_is_on_minus_off(monkeypatch):
    scripted_capture(monkeypatch, [-20.0, -35.0])
    phaser = FakePhaser()
    contrast, on, off = pf.measure_array_contrast(phaser)
    assert (on, off) == (-20.0, -35.0)
    assert contrast == pytest.approx(15.0)
    assert contrast > pf.MIN_ARRAY_CONTRAST_DB


def test_dead_array_reads_as_zero_contrast(monkeypatch):
    """The measured bench: all eight elements off changed nothing."""
    scripted_capture(monkeypatch, [-51.84, -51.84])
    contrast, _, _ = pf.measure_array_contrast(FakePhaser())
    assert contrast == pytest.approx(0.0)
    assert contrast < pf.MIN_ARRAY_CONTRAST_DB


def test_elements_are_left_enabled_afterwards(monkeypatch):
    """Calibration runs straight after, so the array must not be left off."""
    scripted_capture(monkeypatch, [-20.0, -35.0])
    phaser = FakePhaser()
    pf.measure_array_contrast(phaser, max_gain=127)
    assert phaser.gains == [127] * 8
    # And it really did turn them off in between, or it measured nothing.
    assert (0, 0) in phaser.gain_history


def test_a_tone_seen_by_one_step_is_still_trusted():
    """Regression: the gate that rejected a real 72.9 dB tone on the bench.

    How many LO steps see a tone is set by the ANALOG bandwidth, not the
    sample rate. The HB100 search runs rx_rf_bandwidth at 10 MHz behind a
    20 MHz filter and steps 10 MHz, so usable coverage is about +/-5 MHz and
    one step is the normal case, not a warning sign. Requiring two confirming
    steps refused a perfectly good bench, so the default must not.
    """
    m, bb, steps = build_scan(real_tone_hz=None)          # spur + noise only
    bb_narrow = bb
    # Put the tone in exactly one row, as a 10 MHz analog window does.
    row = int(np.argmin(np.abs(steps - 10.450e9)))
    col = int(np.argmin(np.abs(bb_narrow - (REAL_TONE_HZ - steps[row]))))
    m[row, col] = -30.0

    result = pf.find_scan_peak(m, bb_narrow, steps)
    assert result["freq_hz"] == pytest.approx(REAL_TONE_HZ, abs=30e3)
    assert result["steps_confirming"] == 1
    ok, reason = pf.scan_peak_is_trustworthy(result)
    assert ok, reason


def test_step_count_can_still_be_required_explicitly():
    """The gate remains available for a scan whose steps really do overlap."""
    m, bb, steps = build_scan(real_tone_hz=None)
    row = int(np.argmin(np.abs(steps - 10.450e9)))
    col = int(np.argmin(np.abs(bb - (REAL_TONE_HZ - steps[row]))))
    m[row, col] = -30.0
    result = pf.find_scan_peak(m, bb, steps)
    ok, reason = pf.scan_peak_is_trustworthy(result, min_confirming=2)
    assert not ok
    assert "LO step" in reason


def test_spur_only_scan_fails_on_snr_not_step_count():
    """With the step gate off, spur rejection plus SNR must still catch it."""
    m, bb, steps = build_scan(real_tone_hz=None)
    result = pf.find_scan_peak(m, bb, steps)
    ok, reason = pf.scan_peak_is_trustworthy(result, min_confirming=1)
    assert not ok
    assert "no signal" in reason
