"""The simulated CW radar scene, end to end: SimSDR -> process_cw_frame.

Radar mode used to refuse to start under --sim ("CW radar not available in
--sim mode"), so the only way to see the radar UI move was to have a Phaser on
the bench. These tests drive the same path the sim backend now takes -- the
simulator synthesizes Doppler returns, the production DSP recovers velocities
from them -- which is what makes the radar developable without hardware.

Deliberately a round trip rather than a fixture comparison: the simulator
places a target's line at signal_freq + 2*v*f_c/c and the DSP inverts exactly
that relation, so a sign error or a wrong carrier on either side shows up as a
wrong recovered velocity instead of cancelling out.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ADAR_pyadi_functions import ADAR_set_Taper
from phaser_radar_dsp import DEFAULTS, TAPER_PRESETS, process_cw_frame, resolve_taper
from phaser_sim import SimSDR, make_stub_array


# 8k is plenty to resolve a few m/s and keeps the suite fast; the deployed
# default is 64k.
FFT_SIZE = 8192
FS = DEFAULTS["sample_rate"]
SIG = DEFAULTS["signal_freq"]
OUT = DEFAULTS["output_freq"]


def make_sdr(targets, taper="rect"):
    array = make_stub_array()
    ADAR_set_Taper(array, resolve_taper(taper))
    sdr = SimSDR(array, signal_freq=10.5e9, element_spacing=0.014,
                 sample_rate=FS, buffer_size=FFT_SIZE)
    sdr.set_cw_mode(True, signal_freq=SIG, output_freq=OUT, targets=targets)
    return array, sdr


def capture(sdr):
    """What phaser_cw_radar.capture_cw_frame does: sum the two sub-arrays."""
    c0, c1 = sdr.rx()
    return c0 + c1


def frame(sdr, vel_max=30.0):
    return process_cw_frame(capture(sdr), fs=FS, signal_freq=SIG,
                            output_freq=OUT, vel_max=vel_max)


def velocity_resolution():
    return 299_792_458.0 * (FS / FFT_SIZE) / (2 * OUT)


@pytest.mark.parametrize("velocity", [-8.0, -2.5, 3.0, 11.0])
def test_sim_target_velocity_is_recovered(velocity):
    """A single simulated target at v comes back out of the DSP at v."""
    _, sdr = make_sdr([(velocity, 0.0, 1.0)])
    result = frame(sdr)
    dv = velocity_resolution()
    assert abs(result["peak_velocity"] - velocity) < 3 * dv, (
        f"recovered {result['peak_velocity']:.2f} m/s for a {velocity:.2f} m/s target "
        f"(bin width {dv:.3f})"
    )


def test_stationary_leakage_sits_at_zero_velocity():
    """The Tx->Rx leakage every CW radar has must land in the 0 m/s bin --
    it is the reference the workshop teaches you to look past."""
    _, sdr = make_sdr([(0.0, 0.0, 1.0)])
    result = frame(sdr)
    assert abs(result["peak_velocity"]) < 3 * velocity_resolution()


def test_default_scene_shows_a_moving_target_above_the_noise():
    """The out-of-the-box sim scene has to actually look like a radar return,
    or --sim is no use for developing the UI."""
    _, sdr = make_sdr(None)  # falls back to CW_DEFAULT_TARGETS
    result = frame(sdr)
    spec = np.array(result["spectrum_db"])
    vel = np.array(result["velocity_axis"])
    assert np.all(np.isfinite(spec))
    # The 2.5 m/s target in the default scene should be visibly above the floor.
    floor = np.median(spec)
    near = np.abs(vel - 2.5) < 0.5
    assert near.any(), "default scene's moving target is outside the returned window"
    assert spec[near].max() > floor + 10, "moving target is not above the noise floor"


def test_frame_is_json_serializable_and_finite():
    import json
    _, sdr = make_sdr([(4.0, 0.0, 1.0)])
    result = frame(sdr)
    json.dumps(result)
    assert np.all(np.isfinite(np.array(result["spectrum_db"])))
    assert np.all(np.isfinite(np.array(result["velocity_axis"])))


def test_taper_reaches_the_simulated_array():
    """The Rect/Hann/Black buttons were dead: the frontend pushed a taper, the
    backend stored it in a dict, and nothing ever wrote it to the ADAR1000.

    A taper changes the array's aperture illumination, so with the same scene
    the received amplitude has to change with it. Rect (all 127) collects more
    energy than Blackman (edges at 8), so the on-boresight return is stronger.
    """
    scene = [(3.0, 0.0, 1.0)]
    _, rect_sdr = make_sdr(scene, taper="rect")
    _, black_sdr = make_sdr(scene, taper="blackman")

    rect_peak = frame(rect_sdr)["peak_magnitude_db"]
    black_peak = frame(black_sdr)["peak_magnitude_db"]

    assert rect_peak > black_peak + 3, (
        f"taper did not reach the array: rect {rect_peak:.1f} dB vs "
        f"blackman {black_peak:.1f} dB"
    )


def test_taper_is_latched_not_left_in_the_shadow_registers():
    """ADAR1000 gain writes sit in SPI shadow registers until a latch. The sim
    models that, so an unlatched taper would leave latched_gain untouched."""
    array = make_stub_array()
    ADAR_set_Taper(array, resolve_taper("blackman"))
    latched = [array.elements[i + 1].latched_gain for i in range(8)]
    assert latched == TAPER_PRESETS["blackman"]


def test_leaving_cw_mode_restores_the_beamforming_scene():
    """set_cw_mode(False) has to put the simulator back on the HB100 scene,
    or a sweep run after a radar session sees Doppler lines."""
    array, sdr = make_sdr([(5.0, 0.0, 1.0)])
    sdr.set_cw_mode(False)
    c0, c1 = sdr.rx()
    n = len(c0)
    spec = np.abs(np.fft.fftshift(np.fft.fft((c0 + c1) * np.blackman(n))))
    freqs = np.fft.fftshift(np.fft.fftfreq(n, 1.0 / FS))
    peak_hz = freqs[int(np.argmax(spec))]
    # The beamforming scene puts its target at TARGET_IF_HZ, not at the CW IF.
    # At 600 kHz sampling the 1 MHz IF aliases, so compare against the alias.
    expected = ((SimSDR.TARGET_IF_HZ + FS / 2) % FS) - FS / 2
    assert abs(peak_hz - expected) < 5 * (FS / n), (
        f"after leaving CW mode the peak is at {peak_hz:.0f} Hz, expected {expected:.0f} Hz"
    )


def test_tx_buffer_calls_are_survivable():
    """enter_cw_mode loads and tears down a cyclic Tx buffer; the sim has to
    accept both or radar mode cannot start under --sim."""
    _, sdr = make_sdr([(1.0, 0.0, 1.0)])
    sdr.tx([np.zeros(8), np.zeros(8)])
    sdr.tx_destroy_buffer()
    assert sdr.rx() is not None
