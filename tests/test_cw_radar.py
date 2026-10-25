"""Synthetic tests for the CW radar DSP — no hardware needed.

Builds a CW IQ signal at 100 kHz IF plus a Doppler-shifted echo, runs the same
processing the live backend will run, and verifies:
  - velocity axis is symmetric and has expected resolution
  - peak velocity matches the simulated Doppler shift
  - no NaN/Inf, output JSON-serializable
  - the vel_max crop is honoured
  - taper requests resolve to sane ADAR1000 gain codes
  - the CW mixing LO is the sum the RF chain actually needs

Imports phaser_radar_dsp, not phaser_cw_radar. That is the point of the split:
the old import pulled in SDR_functions -> adi, so on any machine without
pyadi-iio this whole file skipped itself and the suite went green having
asserted nothing about the radar at all.
"""

import json
import math
import sys

import numpy as np

from pathlib import Path
# Backend modules live at the repo root, one level up from tests/.
# Anchored to __file__ so this works regardless of the working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# pytest is optional. This file predates the suite and is still meant to run as
# a plain script on the Pi -- which has no pytest -- so importing it
# unconditionally would break the way it is actually used there.
try:
    import pytest
except ImportError:
    pytest = None

from phaser_radar_dsp import (
    DEFAULTS,
    TAPER_PRESETS,
    build_iq_tone,
    cw_lo_freq,
    process_cw_frame,
    resolve_taper,
)


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


# These cases have always been here and have always asserted something real --
# monotonic axis, no NaN, peak velocity within three bins, JSON round-trip --
# but nothing in the file was named test_*, so pytest collected zero tests and
# passed vacuously. main() is kept so the file still runs as a script.
CASES = (-12.0, -2.5, 0.0, 5.0, 17.0)


if pytest is not None:
    @pytest.mark.parametrize("velocity", CASES)
    def test_recovers_simulated_velocity(velocity):
        np.random.seed(0)   # synthesize() adds noise; keep the result decidable
        run_case(velocity, f"v={velocity:+.1f}")


if pytest is not None:
    @pytest.mark.parametrize("vel_max", [5.0, 20.0, 60.0])
    def test_vel_max_controls_the_returned_window(vel_max):
        """The UI's Velocity Max slider has to reach the crop, not just the axis.

        process_cw_frame used to crop at a hardcoded 30 m/s while the frontend
        offered a slider up to 200, so asking for a wider window silently got
        you the same bins and a wider, emptier plot.
        """
        np.random.seed(0)
        fs, n = DEFAULTS["sample_rate"], DEFAULTS["fft_size"]
        iq = synthesize(fs, n, DEFAULTS["signal_freq"], 2.0, DEFAULTS["output_freq"])
        result = process_cw_frame(
            iq, fs=fs, signal_freq=DEFAULTS["signal_freq"],
            output_freq=DEFAULTS["output_freq"], vel_max=vel_max,
        )
        v = np.array(result["velocity_axis"])
        assert np.all(np.abs(v) <= vel_max + 1e-6), "returned bins outside the requested window"
        # And the window is actually filled, not merely bounded: the widest
        # available bin has to be close to the limit we asked for.
        df = fs / n
        dv = 299_792_458.0 * df / (2 * DEFAULTS["output_freq"])
        assert np.max(np.abs(v)) > vel_max - 2 * dv, "window narrower than requested"

    def test_wider_window_returns_more_bins():
        np.random.seed(0)
        fs, n = DEFAULTS["sample_rate"], DEFAULTS["fft_size"]
        iq = synthesize(fs, n, DEFAULTS["signal_freq"], 2.0, DEFAULTS["output_freq"])
        kw = dict(fs=fs, signal_freq=DEFAULTS["signal_freq"],
                  output_freq=DEFAULTS["output_freq"])
        narrow = process_cw_frame(iq, vel_max=10.0, **kw)
        wide = process_cw_frame(iq, vel_max=50.0, **kw)
        assert len(wide["velocity_axis"]) > len(narrow["velocity_axis"])

    @pytest.mark.parametrize("name", sorted(TAPER_PRESETS))
    def test_taper_presets_resolve_to_eight_valid_gain_codes(name):
        gains = resolve_taper(name)
        assert gains == TAPER_PRESETS[name]
        assert len(gains) == 8
        assert all(0 <= g <= 127 for g in gains), "outside the ADAR1000 7-bit field"

    def test_taper_accepts_an_explicit_list_and_clamps_it():
        assert resolve_taper([0, 50, 127, 200, -5, 10, 20, 30]) == [
            0, 50, 127, 127, 0, 10, 20, 30
        ]

    @pytest.mark.parametrize("bad", [None, "nonsense", [], "", ["a", "b"]])
    def test_taper_falls_back_to_blackman_rather_than_raising(bad):
        """A bad taper should cost a sidelobe, not the whole radar mode."""
        assert resolve_taper(bad) == TAPER_PRESETS["blackman"]

    def test_taper_pads_a_short_list_to_eight():
        assert resolve_taper([127, 127]) == [127, 127, 0, 0, 0, 0, 0, 0]

    def test_cw_lo_is_the_sum_the_rf_chain_needs():
        """LO = output_freq + signal_freq + center_freq.

        The Pluto transmits signal_freq of baseband on center_freq; the mixer
        has to put that on output_freq and bring the echo back to center_freq.
        Only the sum satisfies both, and enter_cw_mode never programmed it at
        all before -- the LO stayed on the HB100 receive frequency while the
        velocity math divided by output_freq.
        """
        cfg = dict(DEFAULTS)
        lo = cw_lo_freq(cfg)
        assert lo == cfg["output_freq"] + cfg["signal_freq"] + cfg["center_freq"]
        # Downconverting the echo with that LO lands it on the Pluto's Rx LO
        # plus the IF tone, which is what process_cw_frame expects to find.
        assert lo - cfg["output_freq"] == cfg["center_freq"] + cfg["signal_freq"]

    def test_tone_is_cyclic_and_the_requested_length():
        """A tone that is not a whole number of cycles per buffer smears when
        the hardware wraps the cyclic buffer."""
        fs, n, sig = 600_000, 8192, 100_000
        iq = build_iq_tone(fs, n, sig)
        assert len(iq) == n
        cycles = round(sig / fs * n)
        assert abs(iq[0] - iq[-1] * np.exp(1j * 2 * np.pi * cycles / n)) < 1e-6 * abs(iq[0]) + 1e-6

    def test_recovered_velocity_tracks_the_carrier():
        """Velocity is c*f_d/(2*f_carrier), so the same Doppler line read
        against a different carrier must report a different speed. This is why
        the LO has to actually be where output_freq says it is."""
        np.random.seed(0)
        fs, n, sig = DEFAULTS["sample_rate"], DEFAULTS["fft_size"], DEFAULTS["signal_freq"]
        out_f = DEFAULTS["output_freq"]
        iq = synthesize(fs, n, sig, 4.0, out_f)
        right = process_cw_frame(iq, fs=fs, signal_freq=sig, output_freq=out_f)
        wrong = process_cw_frame(iq, fs=fs, signal_freq=sig, output_freq=out_f / 2)
        assert abs(right["peak_velocity"] - 4.0) < 0.2
        assert abs(wrong["peak_velocity"] - 8.0) < 0.4


def main():
    print("phaser_radar_dsp.process_cw_frame() synthetic test")
    print("-" * 60)
    print(f"  fs={DEFAULTS['sample_rate']} Hz, fft={DEFAULTS['fft_size']}, "
          f"sig_freq={DEFAULTS['signal_freq']} Hz, output_freq={DEFAULTS['output_freq']/1e9} GHz")
    np.random.seed(0)
    for v in CASES:
        run_case(v, f"v={v:+.1f}")
    print("\nAll cases passed.")


if __name__ == "__main__":
    main()
