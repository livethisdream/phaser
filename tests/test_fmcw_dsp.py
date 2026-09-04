"""The FMCW radar DSP, against synthetic beat signals — no hardware needed.

Covers the four FMCW labs' processing (docs/2025_Phaser_labs_Python.pdf,
pp. 29-38): beat-frequency range, range-Doppler, MTI pulse cancellation, and
CFAR. Every test builds a signal whose answer is known from the physics and
checks the DSP recovers it, rather than pinning whatever the code happens to
emit today.

The synthesis here is deliberately independent of phaser_sim's: if both sides
shared a helper, a sign error in the shared half would cancel and the tests
would pass on broken code. tests/test_fmcw_sim.py closes the loop the other
way, driving the simulator into the same DSP.
"""

import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phaser_radar_dsp import (
    C,
    FMCW_DEFAULTS,
    apply_cfar,
    beat_freq_to_range,
    ca_cfar,
    chirp_matrix,
    fmcw_slope,
    max_unambiguous_range,
    max_unambiguous_velocity,
    mti_filter,
    range_doppler_map,
    range_profile,
    range_resolution,
    range_to_beat_freq,
    velocity_resolution,
)

FS = 600_000.0
CHIRP_BW = 500e6
RAMP_TIME = 1e-3
PRI = 1e-3
OUTPUT_FREQ = 12.2e9
SIGNAL_FREQ = 100_000.0
SPC = 600          # samples per chirp at 600 kHz over a 1 ms ramp


def beat_signal(ranges_velocities, n_samples, num_chirps=1, fs=FS,
                signal_freq=SIGNAL_FREQ, snr_db=30.0, seed=0):
    """Chirp-major beat signal for a list of (range_m, velocity_mps, amp)."""
    spc = n_samples // num_chirps
    t_fast = np.arange(spc) / fs
    m_idx = np.arange(num_chirps)
    slope = CHIRP_BW / RAMP_TIME

    total = np.zeros((num_chirps, spc), dtype=complex)
    for target_range, velocity, amp in ranges_velocities:
        f_b = 2.0 * slope * target_range / C
        f_d = 2.0 * velocity * OUTPUT_FREQ / C
        fast = 2 * np.pi * (signal_freq + f_b + f_d) * t_fast
        slow = 2 * np.pi * f_d * m_idx * PRI
        total += amp * np.exp(1j * (fast[None, :] + slow[:, None]))

    flat = total.ravel()
    if snr_db is not None:
        rng = np.random.default_rng(seed)
        p_sig = np.mean(np.abs(flat) ** 2)
        sigma = math.sqrt(p_sig / (10 ** (snr_db / 10)))
        flat = flat + (rng.normal(0, 1, flat.size)
                       + 1j * rng.normal(0, 1, flat.size)) * (sigma / math.sqrt(2))
    return flat * (2 ** 11)


# --- the range relation -------------------------------------------------

def test_beat_frequency_matches_the_labs_3_3_khz_per_metre():
    """The labs state 3.3 kHz of beat per metre at B=500 MHz, T=1 ms."""
    per_metre = range_to_beat_freq(1.0, CHIRP_BW, RAMP_TIME)
    assert 3300 < per_metre < 3400, f"{per_metre:.0f} Hz/m"


def test_range_and_beat_frequency_round_trip():
    for r in (0.5, 1.0, 7.25, 30.0):
        f_b = range_to_beat_freq(r, CHIRP_BW, RAMP_TIME)
        assert abs(float(beat_freq_to_range(f_b, CHIRP_BW, RAMP_TIME)) - r) < 1e-9


def test_slope_is_bandwidth_over_ramp_time():
    assert fmcw_slope(500e6, 1e-3) == pytest.approx(5e11)
    # Halving the ramp time doubles the slope, so the same range beats twice
    # as high -- the lab's "try changing ramp time to 500 us".
    assert range_to_beat_freq(1.0, CHIRP_BW, 500e-6) == pytest.approx(
        2 * range_to_beat_freq(1.0, CHIRP_BW, RAMP_TIME))


def test_range_resolution_depends_only_on_bandwidth():
    """c/(2B): the lab's 'change chirp_BW to 200e6, what happens to dot size?'"""
    assert range_resolution(500e6) == pytest.approx(0.2998, abs=1e-3)
    assert range_resolution(200e6) > range_resolution(500e6)
    # Ramp time does not enter into it.
    assert range_resolution(500e6) == range_resolution(500e6)


def test_max_range_is_set_by_nyquist_on_the_beat():
    r_max = max_unambiguous_range(FS, CHIRP_BW, RAMP_TIME)
    assert beat_freq_to_range(FS / 2, CHIRP_BW, RAMP_TIME) == pytest.approx(r_max)


# --- range profile ------------------------------------------------------

@pytest.mark.parametrize("target_range", [1.0, 2.0, 5.0, 12.0])
def test_range_profile_finds_a_single_target(target_range):
    iq = beat_signal([(target_range, 0.0, 1.0)], SPC)
    out = range_profile(iq, FS, CHIRP_BW, RAMP_TIME,
                        zero_range_freq=SIGNAL_FREQ, range_max=20.0)
    # One FFT bin is fs/n Hz, which converts to this many metres.
    bin_m = float(beat_freq_to_range(FS / SPC, CHIRP_BW, RAMP_TIME))
    assert abs(out["peak_range"] - target_range) < 2 * bin_m, (
        f"found {out['peak_range']:.2f} m, expected {target_range:.2f} m "
        f"(bin {bin_m:.2f} m)")


def test_range_profile_axis_is_monotonic_and_cropped():
    iq = beat_signal([(4.0, 0.0, 1.0)], SPC)
    out = range_profile(iq, FS, CHIRP_BW, RAMP_TIME,
                        zero_range_freq=SIGNAL_FREQ, range_max=15.0)
    axis = np.array(out["range_axis"])
    assert np.all(np.diff(axis) > 0)
    assert axis.min() >= 0.0
    assert axis.max() <= 15.0
    assert len(axis) == len(out["profile_db"])
    assert np.all(np.isfinite(np.array(out["profile_db"])))


def test_zero_range_calibration_shifts_the_axis():
    """The lab holds a target at 0 m, reads the actual peak frequency, and uses
    it as the 0 m reference because the IF tone is not exactly signal_freq."""
    iq = beat_signal([(0.0, 0.0, 1.0)], SPC, signal_freq=103_000.0)
    uncalibrated = range_profile(iq, FS, CHIRP_BW, RAMP_TIME,
                                 zero_range_freq=SIGNAL_FREQ, range_max=20.0)
    calibrated = range_profile(iq, FS, CHIRP_BW, RAMP_TIME,
                               zero_range_freq=103_000.0, range_max=20.0)
    assert abs(calibrated["peak_range"]) < 0.35
    assert uncalibrated["peak_range"] > calibrated["peak_range"] + 0.5


def test_range_profile_separates_two_resolvable_targets():
    iq = beat_signal([(2.0, 0.0, 1.0), (8.0, 0.0, 1.0)], SPC, snr_db=40)
    out = range_profile(iq, FS, CHIRP_BW, RAMP_TIME,
                        zero_range_freq=SIGNAL_FREQ, range_max=20.0)
    axis = np.array(out["range_axis"])
    prof = np.array(out["profile_db"])
    floor = np.median(prof)
    for expected in (2.0, 8.0):
        near = np.abs(axis - expected) < 0.6
        assert near.any(), f"no bins near {expected} m"
        assert prof[near].max() > floor + 15, f"no return at {expected} m"


# --- chirp matrix -------------------------------------------------------

def test_chirp_matrix_is_fast_time_by_slow_time():
    """The labs' N x M: N samples per chirp down, M chirps across."""
    iq = np.arange(12)
    m = chirp_matrix(iq, num_chirps=3)
    assert m.shape == (4, 3)
    # Column 0 must be the first chirp's four samples, in order.
    assert list(m[:, 0]) == [0, 1, 2, 3]
    assert list(m[:, 1]) == [4, 5, 6, 7]


def test_chirp_matrix_drops_a_partial_trailing_chirp():
    """Zero-padding a partial chirp would plant a false zero-Doppler return."""
    m = chirp_matrix(np.arange(14), num_chirps=3)
    assert m.shape == (4, 3)
    assert 12 not in m and 13 not in m


def test_chirp_matrix_rejects_impossible_splits():
    with pytest.raises(ValueError):
        chirp_matrix(np.arange(4), num_chirps=0)
    with pytest.raises(ValueError):
        chirp_matrix(np.arange(2), num_chirps=8)


# --- range-Doppler ------------------------------------------------------

@pytest.mark.parametrize("velocity", [-4.0, 0.0, 2.5])
def test_range_doppler_recovers_range_and_velocity(velocity):
    num_chirps = 64
    iq = beat_signal([(5.0, velocity, 1.0)], SPC * num_chirps, num_chirps)
    out = range_doppler_map(chirp_matrix(iq, num_chirps), FS, CHIRP_BW,
                            RAMP_TIME, PRI, OUTPUT_FREQ,
                            zero_range_freq=SIGNAL_FREQ, range_max=20.0)
    dv = out["velocity_resolution"]
    bin_m = float(beat_freq_to_range(FS / SPC, CHIRP_BW, RAMP_TIME))
    assert abs(out["peak_range"] - 5.0) < 2 * bin_m
    assert abs(out["peak_velocity"] - velocity) < 2 * dv, (
        f"found {out['peak_velocity']:.2f} m/s, expected {velocity:.2f} "
        f"(resolution {dv:.2f})")


def test_range_doppler_map_shape_matches_its_axes():
    num_chirps = 32
    iq = beat_signal([(4.0, 1.0, 1.0)], SPC * num_chirps, num_chirps)
    out = range_doppler_map(chirp_matrix(iq, num_chirps), FS, CHIRP_BW,
                            RAMP_TIME, PRI, OUTPUT_FREQ,
                            zero_range_freq=SIGNAL_FREQ, range_max=20.0)
    m = np.array(out["map_db"])
    assert m.shape == (len(out["range_axis"]), len(out["velocity_axis"]))
    assert np.all(np.isfinite(m))


def test_more_chirps_sharpen_the_velocity_resolution():
    """The lab's 'change num_chirps to 32 or 128' question."""
    coarse = velocity_resolution(32, PRI, OUTPUT_FREQ)
    fine = velocity_resolution(128, PRI, OUTPUT_FREQ)
    assert fine < coarse
    assert fine == pytest.approx(coarse / 4)


def test_velocity_beyond_nyquist_aliases():
    """Past c/(4*f_c*PRI) the Doppler folds; worth knowing before a lab asks
    why a fast fidget spinner reads backwards."""
    v_max = max_unambiguous_velocity(PRI, OUTPUT_FREQ)
    num_chirps = 64
    over = v_max * 1.5
    iq = beat_signal([(5.0, over, 1.0)], SPC * num_chirps, num_chirps, snr_db=40)
    out = range_doppler_map(chirp_matrix(iq, num_chirps), FS, CHIRP_BW,
                            RAMP_TIME, PRI, OUTPUT_FREQ,
                            zero_range_freq=SIGNAL_FREQ, range_max=20.0)
    assert abs(out["peak_velocity"]) <= v_max + 1e-6
    assert out["peak_velocity"] < 0, "an over-Nyquist closing target folds negative"


# --- MTI ----------------------------------------------------------------

def test_mti_none_is_a_passthrough():
    m = np.arange(12).reshape(4, 3)
    assert np.array_equal(mti_filter(m, "none"), m)
    assert np.array_equal(mti_filter(m, None), m)


def test_mti_costs_one_chirp_per_stage():
    m = np.zeros((10, 8))
    assert mti_filter(m, "2pulse").shape == (10, 7)
    assert mti_filter(m, "3pulse").shape == (10, 6)


def test_two_pulse_mti_cancels_a_stationary_target():
    """A stationary return is identical chirp to chirp, so differencing kills
    it. This is the whole claim of the MTI lab."""
    num_chirps = 32
    n = SPC * num_chirps
    still = chirp_matrix(beat_signal([(5.0, 0.0, 1.0)], n, num_chirps, snr_db=None),
                         num_chirps)
    residual = np.abs(mti_filter(still, "2pulse")).mean()
    assert residual < 0.02 * np.abs(still).mean(), (
        f"stationary target survived MTI: {residual:.3g} vs {np.abs(still).mean():.3g}")


def test_two_pulse_mti_keeps_a_moving_target():
    num_chirps = 32
    n = SPC * num_chirps
    moving = chirp_matrix(beat_signal([(5.0, 3.0, 1.0)], n, num_chirps, snr_db=None),
                          num_chirps)
    residual = np.abs(mti_filter(moving, "2pulse")).mean()
    assert residual > 0.3 * np.abs(moving).mean(), "moving target was cancelled too"


def test_mti_lifts_a_moving_target_out_of_stationary_clutter():
    """The end-to-end claim: strong clutter at zero Doppler, weak mover behind
    it, and MTI is what makes the mover the peak."""
    num_chirps = 64
    n = SPC * num_chirps
    scene = [(2.0, 0.0, 1.0), (6.0, 2.5, 0.15)]   # clutter 16 dB above the mover
    iq = beat_signal(scene, n, num_chirps, snr_db=45)
    matrix = chirp_matrix(iq, num_chirps)

    kw = dict(sample_rate=FS, chirp_bw=CHIRP_BW, ramp_time=RAMP_TIME, pri=PRI,
              output_freq=OUTPUT_FREQ, zero_range_freq=SIGNAL_FREQ, range_max=20.0)
    plain = range_doppler_map(matrix, **kw)
    filtered = range_doppler_map(mti_filter(matrix, "2pulse"), **kw)

    assert abs(plain["peak_range"] - 2.0) < 0.6, "clutter should dominate unfiltered"
    assert abs(filtered["peak_range"] - 6.0) < 0.6, (
        f"after MTI the mover should dominate, got {filtered['peak_range']:.2f} m")
    assert abs(filtered["peak_velocity"] - 2.5) < 1.0


def test_three_pulse_notches_zero_doppler_harder_than_two():
    num_chirps = 32
    n = SPC * num_chirps
    still = chirp_matrix(beat_signal([(5.0, 0.0, 1.0)], n, num_chirps, snr_db=None),
                         num_chirps)
    two = np.abs(mti_filter(still, "2pulse")).mean()
    three = np.abs(mti_filter(still, "3pulse")).mean()
    assert three <= two * 1.05


def test_mti_rejects_an_unknown_mode_and_too_few_chirps():
    with pytest.raises(ValueError):
        mti_filter(np.zeros((4, 4)), "4pulse")
    with pytest.raises(ValueError):
        mti_filter(np.zeros((4, 1)), "2pulse")
    with pytest.raises(ValueError):
        mti_filter(np.zeros((4, 2)), "3pulse")


# --- CFAR ---------------------------------------------------------------

def test_cfar_threshold_tracks_the_noise_floor():
    rng = np.random.default_rng(0)
    noise = 10 * np.log10(rng.exponential(1.0, 512))
    threshold = ca_cfar(noise, num_guard=4, num_ref=16, bias_db=8.0)
    assert threshold.shape == noise.shape
    assert np.all(np.isfinite(threshold))
    # With an 8 dB bias over a mean-power estimate, only a small tail of pure
    # noise should cross -- that is what "constant false alarm rate" buys.
    false_alarms = np.mean(noise > threshold)
    assert false_alarms < 0.05, f"false alarm rate {false_alarms:.3f}"


def test_cfar_detects_a_target_above_the_floor():
    rng = np.random.default_rng(1)
    spectrum = 10 * np.log10(rng.exponential(1.0, 512))
    spectrum[200] = 30.0
    threshold = ca_cfar(spectrum, num_guard=4, num_ref=16, bias_db=8.0)
    _, detections = apply_cfar(spectrum, threshold)
    assert 200 in detections


def test_guard_cells_stop_a_target_raising_its_own_threshold():
    """Without guard cells a broad target's skirts inflate the noise estimate
    at its own peak, which is exactly what the lab has you tune away."""
    spectrum = np.full(512, -20.0)
    spectrum[248:253] = 20.0          # a target several cells wide
    no_guard = ca_cfar(spectrum, num_guard=0, num_ref=16, bias_db=6.0)
    guarded = ca_cfar(spectrum, num_guard=6, num_ref=16, bias_db=6.0)
    assert guarded[250] < no_guard[250]


def test_higher_bias_yields_fewer_detections():
    """The lab's central trade: raise the bias for fewer false alarms and more
    missed targets."""
    rng = np.random.default_rng(2)
    spectrum = 10 * np.log10(rng.exponential(1.0, 512))
    spectrum[100] = 12.0
    low = apply_cfar(spectrum, ca_cfar(spectrum, bias_db=2.0))[1]
    high = apply_cfar(spectrum, ca_cfar(spectrum, bias_db=15.0))[1]
    assert len(high) < len(low)


def test_cfar_does_not_wrap_across_the_ends():
    """A strong near-range return must not set the threshold at max range."""
    spectrum = np.full(256, -30.0)
    spectrum[0:4] = 40.0
    threshold = ca_cfar(spectrum, num_guard=2, num_ref=8, bias_db=6.0)
    assert threshold[-1] < -15.0, "far end was contaminated by the near-range target"


def test_apply_cfar_blanks_below_the_threshold():
    spectrum = np.array([-30.0, -30.0, 10.0, -30.0, -30.0])
    threshold = np.full(5, 0.0)
    masked, detections = apply_cfar(spectrum, threshold)
    assert list(detections) == [2]
    assert masked[2] == 10.0
    assert masked[0] == -30.0


def test_cfar_rejects_bad_parameters():
    with pytest.raises(ValueError):
        ca_cfar(np.zeros(10), num_ref=0)
    with pytest.raises(ValueError):
        ca_cfar(np.zeros((4, 4)))


# --- defaults -----------------------------------------------------------

def test_fmcw_defaults_are_the_labs_values():
    assert FMCW_DEFAULTS["chirp_bw"] == 500e6
    assert FMCW_DEFAULTS["ramp_time"] == 1e-3
    assert FMCW_DEFAULTS["mti"] == "none"
