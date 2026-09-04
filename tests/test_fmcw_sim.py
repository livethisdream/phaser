"""The simulated FMCW radar, end to end: SimSDR -> phaser_radar_dsp.

The point of these is the round trip. phaser_sim places a target's beat at
signal_freq + 2*S*R/c with a slow-time phase advancing at 2*v*f_c/c per PRI;
phaser_radar_dsp inverts exactly those two relations. Neither side imports the
other's arithmetic, so a sign error or a wrong constant in either shows up as a
target in the wrong place rather than cancelling out.

tests/test_fmcw_dsp.py checks the DSP against its own independent synthesis.
This file checks the simulator, and is what makes FMCW developable before the
ADF4159 ramp exists.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ADAR_pyadi_functions import ADAR_set_Taper
from phaser_radar_dsp import (
    beat_freq_to_range,
    chirp_matrix,
    mti_filter,
    range_doppler_map,
    range_profile,
    resolve_taper,
    velocity_resolution,
)
from phaser_sim import SimSDR, make_stub_array

FS = 600_000.0
CHIRP_BW = 500e6
RAMP_TIME = 1e-3
PRI = 1e-3
OUTPUT_FREQ = 12.2e9
SIGNAL_FREQ = 100_000.0
SPC = 600


def make_sdr(targets, num_chirps=1, taper="rect", **overrides):
    array = make_stub_array()
    ADAR_set_Taper(array, resolve_taper(taper))
    sdr = SimSDR(array, signal_freq=10.5e9, element_spacing=0.014,
                 sample_rate=FS, buffer_size=SPC * num_chirps)
    params = dict(chirp_bw=CHIRP_BW, ramp_time=RAMP_TIME, pri=PRI,
                  num_chirps=num_chirps, signal_freq=SIGNAL_FREQ,
                  output_freq=OUTPUT_FREQ, targets=targets)
    params.update(overrides)
    sdr.set_fmcw_mode(True, **params)
    return array, sdr


def capture(sdr):
    """What the backend's capture will do: sum the two sub-arrays."""
    c0, c1 = sdr.rx()
    return c0 + c1


def range_bin_metres():
    return float(beat_freq_to_range(FS / SPC, CHIRP_BW, RAMP_TIME))


DSP_KW = dict(sample_rate=FS, chirp_bw=CHIRP_BW, ramp_time=RAMP_TIME,
              zero_range_freq=SIGNAL_FREQ, range_max=20.0)


# --- range --------------------------------------------------------------

@pytest.mark.parametrize("target_range", [1.0, 3.0, 7.5, 15.0])
def test_sim_target_range_is_recovered(target_range):
    _, sdr = make_sdr([(target_range, 0.0, 0.0, 1.0)])
    out = range_profile(capture(sdr), **DSP_KW)
    assert abs(out["peak_range"] - target_range) < 2 * range_bin_metres(), (
        f"recovered {out['peak_range']:.2f} m for a target at {target_range:.2f} m")


def test_sim_default_scene_shows_its_targets():
    """The out-of-the-box scene has to look like a radar return, or --sim is no
    use for building the range UI."""
    _, sdr = make_sdr(None)
    out = range_profile(capture(sdr), **DSP_KW)
    axis = np.array(out["range_axis"])
    prof = np.array(out["profile_db"])
    assert np.all(np.isfinite(prof))
    floor = np.median(prof)
    for expected in (1.0, 3.5, 7.0):   # SimSDR.FMCW_DEFAULT_TARGETS
        near = np.abs(axis - expected) < 0.6
        assert near.any(), f"no bins near {expected} m"
        assert prof[near].max() > floor + 10, f"no return at {expected} m"


def test_sim_range_profile_is_json_serializable():
    import json
    _, sdr = make_sdr([(4.0, 0.0, 0.0, 1.0)])
    json.dumps(range_profile(capture(sdr), **DSP_KW))


# --- range-Doppler ------------------------------------------------------

@pytest.mark.parametrize("velocity", [-3.0, 0.0, 2.0])
def test_sim_range_doppler_recovers_both_axes(velocity):
    num_chirps = 64
    _, sdr = make_sdr([(6.0, velocity, 0.0, 1.0)], num_chirps=num_chirps)
    matrix = chirp_matrix(capture(sdr), num_chirps)
    out = range_doppler_map(matrix, pri=PRI, output_freq=OUTPUT_FREQ, **DSP_KW)

    dv = velocity_resolution(num_chirps, PRI, OUTPUT_FREQ)
    assert abs(out["peak_range"] - 6.0) < 2 * range_bin_metres()
    assert abs(out["peak_velocity"] - velocity) < 2 * dv, (
        f"recovered {out['peak_velocity']:+.2f} m/s for a {velocity:+.2f} m/s target")


def test_sim_map_shape_matches_its_axes():
    num_chirps = 32
    _, sdr = make_sdr([(5.0, 1.0, 0.0, 1.0)], num_chirps=num_chirps)
    out = range_doppler_map(chirp_matrix(capture(sdr), num_chirps),
                            pri=PRI, output_freq=OUTPUT_FREQ, **DSP_KW)
    m = np.array(out["map_db"])
    assert m.shape == (len(out["range_axis"]), len(out["velocity_axis"]))
    assert np.all(np.isfinite(m))


def test_sim_mti_lifts_a_mover_out_of_the_sim_clutter():
    """The MTI lab, driven entirely by the simulator: strong stationary
    reflector, weak mover further out, and MTI is what promotes the mover."""
    num_chirps = 64
    scene = [(2.0, 0.0, 0.0, 1.0), (7.0, 2.0, 0.0, 0.15)]
    _, sdr = make_sdr(scene, num_chirps=num_chirps)
    matrix = chirp_matrix(capture(sdr), num_chirps)

    plain = range_doppler_map(matrix, pri=PRI, output_freq=OUTPUT_FREQ, **DSP_KW)
    filtered = range_doppler_map(mti_filter(matrix, "2pulse"),
                                 pri=PRI, output_freq=OUTPUT_FREQ, **DSP_KW)

    assert abs(plain["peak_range"] - 2.0) < 0.7, "the stationary return should dominate"
    assert abs(filtered["peak_range"] - 7.0) < 0.7, (
        f"after MTI the mover should dominate, got {filtered['peak_range']:.2f} m")
    assert abs(filtered["peak_velocity"] - 2.0) < 1.0


# --- the array still behaves like an array ------------------------------

def test_taper_reaches_the_simulated_radar_array():
    """The FMCW scene goes through the same per-element superposition as the
    beamforming scene, so the aperture illumination still matters."""
    scene = [(4.0, 0.0, 0.0, 1.0)]
    _, rect = make_sdr(scene, taper="rect")
    _, black = make_sdr(scene, taper="blackman")
    rect_db = range_profile(capture(rect), **DSP_KW)["peak_magnitude_db"]
    black_db = range_profile(capture(black), **DSP_KW)["peak_magnitude_db"]
    assert rect_db > black_db + 3, f"rect {rect_db:.1f} dB vs blackman {black_db:.1f} dB"


def test_an_off_boresight_target_is_weaker_than_one_on_boresight():
    """An unsteered array hears boresight best; the lab's step 23 has you steer
    away from a target and watch the return fall."""
    on = make_sdr([(5.0, 0.0, 0.0, 1.0)])[1]
    off = make_sdr([(5.0, 0.0, 40.0, 1.0)])[1]
    on_db = range_profile(capture(on), **DSP_KW)["peak_magnitude_db"]
    off_db = range_profile(capture(off), **DSP_KW)["peak_magnitude_db"]
    assert on_db > off_db + 3, f"boresight {on_db:.1f} dB vs 40 deg {off_db:.1f} dB"


# --- mode interlocks ----------------------------------------------------

def test_fmcw_and_cw_are_mutually_exclusive():
    """Both modes reconfigure the same SDR; the simulator must not pretend to
    be in two at once."""
    _, sdr = make_sdr([(3.0, 0.0, 0.0, 1.0)])
    assert sdr._fmcw_enable and not sdr._cw_enable

    sdr.set_cw_mode(True, signal_freq=SIGNAL_FREQ, output_freq=OUTPUT_FREQ)
    assert sdr._cw_enable and not sdr._fmcw_enable

    sdr.set_fmcw_mode(True)
    assert sdr._fmcw_enable and not sdr._cw_enable


def test_leaving_fmcw_restores_the_beamforming_scene():
    _, sdr = make_sdr([(5.0, 0.0, 0.0, 1.0)])
    sdr.set_fmcw_mode(False)
    c0, c1 = sdr.rx()
    n = len(c0)
    spec = np.abs(np.fft.fftshift(np.fft.fft((c0 + c1) * np.blackman(n))))
    freqs = np.fft.fftshift(np.fft.fftfreq(n, 1.0 / FS))
    peak_hz = freqs[int(np.argmax(spec))]
    # The beamforming scene's 1 MHz IF aliases at 600 kHz sampling.
    expected = ((SimSDR.TARGET_IF_HZ + FS / 2) % FS) - FS / 2
    assert abs(peak_hz - expected) < 5 * (FS / n)


def test_bad_target_entries_fall_back_to_the_default_scene():
    _, sdr = make_sdr([(1.0, 2.0)])   # too few fields
    assert sdr._fmcw_targets == list(SimSDR.FMCW_DEFAULT_TARGETS)


def test_a_buffer_that_is_not_whole_chirps_still_produces_a_frame():
    """The backend sizes the buffer, and an off-by-a-few must not crash a lab."""
    array = make_stub_array()
    ADAR_set_Taper(array, resolve_taper("rect"))
    sdr = SimSDR(array, signal_freq=10.5e9, element_spacing=0.014,
                 sample_rate=FS, buffer_size=SPC * 8 + 37)
    sdr.set_fmcw_mode(True, chirp_bw=CHIRP_BW, ramp_time=RAMP_TIME, pri=PRI,
                      num_chirps=8, signal_freq=SIGNAL_FREQ,
                      output_freq=OUTPUT_FREQ, targets=[(4.0, 1.0, 0.0, 1.0)])
    c0, c1 = sdr.rx()
    assert len(c0) == SPC * 8 + 37
    assert np.all(np.isfinite(np.abs(c0 + c1)))
