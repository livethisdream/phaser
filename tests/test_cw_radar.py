"""Synthetic test for phaser_cw_radar.process_cw_frame() — no hardware needed.

Builds a CW IQ signal at 100 kHz IF plus a Doppler-shifted echo, runs the same
processing the live backend will run, and verifies:
  - velocity axis is symmetric and has expected resolution
  - peak velocity matches the simulated Doppler shift
  - no NaN/Inf, output JSON-serializable
"""

import json
import math
import sys

import numpy as np

from pathlib import Path
# Backend modules live at the repo root, one level up from tests/.
# Anchored to __file__ so this works regardless of the working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from phaser_cw_radar import process_cw_frame, DEFAULTS


def synthesize(fs, n, signal_freq, doppler_velocity_mps, output_freq, snr_db=20):
    """IF tone at signal_freq + an echo Doppler-shifted by `doppler_velocity_mps`."""
    c = 299_792_458.0
    f_doppler = 2.0 * doppler_velocity_mps * output_freq / c  # Hz
    t = np.arange(n) / fs
    # Direct (leakage) tone at IF
    iq_direct = 0.3 * np.exp(1j * 2 * np.pi * signal_freq * t)
    # Reflection from a moving target
    iq_target = 1.0 * np.exp(1j * 2 * np.pi * (signal_freq + f_doppler) * t)
    sig = iq_direct + iq_target
    # AWGN
    p_signal = np.mean(np.abs(sig) ** 2)
    sigma = math.sqrt(p_signal / (10 ** (snr_db / 10)))
    noise = (np.random.randn(n) + 1j * np.random.randn(n)) * (sigma / math.sqrt(2))
    return (sig + noise) * (2 ** 11)  # bring up to ad9361 fixed-point levels


def run_case(velocity, label):
    fs = DEFAULTS["sample_rate"]
    n = DEFAULTS["fft_size"]
    sig_f = DEFAULTS["signal_freq"]
    out_f = DEFAULTS["output_freq"]

    iq = synthesize(fs, n, sig_f, velocity, out_f, snr_db=20)
    result = process_cw_frame(iq, fs=fs, signal_freq=sig_f, output_freq=out_f,
                              fft_window="blackman", downsample_iq=512)

    # Validate basics
    v_axis = np.array(result["velocity_axis"])
    spec = np.array(result["spectrum_db"])
    assert v_axis.ndim == 1
    assert spec.ndim == 1
    assert len(v_axis) == len(spec)
    assert np.all(np.isfinite(spec)), "spectrum has NaN/Inf"
    # axis monotonically increasing
    assert np.all(np.diff(v_axis) > 0), "velocity axis not monotonic"
    # symmetric (within rounding)
    assert abs(v_axis[0] + v_axis[-1]) < 1.0
    # peak velocity should match simulated within velocity resolution
    df = fs / n  # Hz/bin
    dv = 299_792_458.0 * df / (2 * out_f)  # m/s/bin
    err = abs(result["peak_velocity"] - velocity)
    print(f"  [{label}] expected v={velocity:+.3f} m/s, got {result['peak_velocity']:+.3f} m/s "
          f"(error {err:.4f}, bin width {dv:.4f} m/s, peak {result['peak_magnitude_db']:.1f} dB, "
          f"axis bins {len(v_axis)})")
    assert err < 3 * dv, f"peak velocity off by more than 3 bins ({err:.3f} vs {dv:.3f})"
    # JSON-serializable round-trip
    json.dumps(result)


def main():
    print("phaser_cw_radar.process_cw_frame() synthetic test")
    print("-" * 60)
    print(f"  fs={DEFAULTS['sample_rate']} Hz, fft={DEFAULTS['fft_size']}, "
          f"sig_freq={DEFAULTS['signal_freq']} Hz, output_freq={DEFAULTS['output_freq']/1e9} GHz")
    np.random.seed(0)
    for v in (-12.0, -2.5, 0.0, 5.0, 17.0):
        run_case(v, f"v={v:+.1f}")
    print("\nAll cases passed.")


if __name__ == "__main__":
    main()
