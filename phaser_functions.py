import os
import pickle

import numpy as np


REPO_DIR = os.path.dirname(__file__)


def _repo_path(filename):
    return os.path.join(REPO_DIR, filename)


def _load_pickle_file(filename, default_value):
    path = _repo_path(filename)
    try:
        with open(path, "rb") as file:
            return pickle.load(file)
    except FileNotFoundError:
        return default_value


def load_hb100_cal():
    """Load HB100 calibration signal frequency if it exists."""
    cal_file = _repo_path("hb100_cal.txt")
    if os.path.exists(cal_file):
        with open(cal_file, "r", encoding="utf-8") as f:
            try:
                return float(f.read().strip())
            except ValueError:
                pass
    raise FileNotFoundError("Calibration file not found or invalid.")


def save_hb100_cal(freq_hz):
    cal_file = _repo_path("hb100_cal.txt")
    with open(cal_file, "w", encoding="utf-8") as f:
        f.write(str(float(freq_hz)))


def spec_est(data, sample_rate, ref=2**12, plot=False):
    """Windowed FFT magnitude spectrum in dBFS, plus the frequency axis.

    Restored after commit b66125a stripped 300 lines from this module -- it
    removed spec_est while leaving phaser_find_hb100_headless.py's
    `from phaser_functions import save_hb100_cal, spec_est` in place. The
    breakage stayed hidden because the Pi carried pyadi-iio's own fuller copy
    of this file; it only surfaced once deploys began overwriting that copy
    with this one, at which point the HB100 search died at import.
    """
    samples = np.asarray(data)
    if samples.size == 0:
        return np.empty(0), np.empty(0)

    window = np.blackman(samples.size)
    spectrum = np.fft.fft(samples * window)
    magnitude = np.abs(spectrum) / max(np.sum(window), 1)
    # Floor before the log so an empty bin cannot produce -inf.
    magnitude = np.maximum(magnitude, 1e-15)
    amplitude = 20 * np.log10(magnitude / ref)
    freqs = np.fft.fftfreq(samples.size, d=1 / float(sample_rate))

    if plot:
        import matplotlib.pyplot as plt

        plt.figure()
        plt.plot(np.fft.fftshift(freqs), np.fft.fftshift(amplitude))
        plt.xlabel("Frequency (Hz)")
        plt.ylabel("Amplitude (dBFS)")
        plt.title("Spectrum Estimate")
        plt.show()

    return amplitude, freqs


# --- array calibration ------------------------------------------------------
#
# Restored after commit b66125a removed these along with spec_est, leaving
# phaser_cal_headless.py importing three names that no longer existed. The
# breakage stayed hidden while the Pi carried pyadi-iio's fuller copy of this
# module, and surfaced once deploys began overwriting it.
#
# These only COMPUTE and stash results on the phaser object (`ccal`, `gcal`,
# `pcal`). Persisting them is pyadi-iio's job: phaser_cal_headless.py calls
# my_phaser.save_channel_cal() and friends, CN0566 methods that pickle to
# channel_cal_val.pkl / gain_cal_val.pkl / phase_cal_val.pkl -- exactly what
# ADAR_pyadi_functions and SDR_functions read back. So there is deliberately no
# save_*_cal here; adding one would create a second, competing writer.


def _rx_channels(phaser):
    """The two receive channels as arrays, whatever shape rx() hands back."""
    raw = phaser.sdr.rx()
    if isinstance(raw, np.ndarray):
        if raw.ndim == 1:
            return raw, raw
        return raw[0], raw[1]
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        return np.asarray(raw[0]), np.asarray(raw[1])
    arr = np.asarray(raw)
    return arr, arr


def _capture_peak_dbfs(phaser, averages=4):
    """Mean peak of the summed-channel spectrum over `averages` captures."""
    peaks = []
    last_spectrum = None
    for _ in range(max(1, averages)):
        ch0, ch1 = _rx_channels(phaser)
        data_sum = ch0 + ch1
        spectrum, _ = spec_est(data_sum, phaser.sdr.sample_rate, ref=2**11, plot=False)
        last_spectrum = np.fft.fftshift(spectrum)
        peaks.append(float(np.max(last_spectrum)))
    return float(np.mean(peaks)), last_spectrum


def channel_calibration(phaser, verbose=False, averages=8):
    """Estimate SDR channel gain mismatch in dB and store it in phaser.ccal."""
    rms0 = []
    rms1 = []
    for _ in range(max(1, averages)):
        ch0, ch1 = _rx_channels(phaser)
        rms0.append(np.sqrt(np.mean(np.abs(ch0) ** 2)))
        rms1.append(np.sqrt(np.mean(np.abs(ch1) ** 2)))

    # Floored so a dead channel yields a large correction rather than a
    # divide-by-zero or a -inf that would poison everything downstream.
    avg0 = max(float(np.mean(rms0)), 1e-15)
    avg1 = max(float(np.mean(rms1)), 1e-15)
    correction_db = 20 * np.log10(avg0 / avg1)
    phaser.ccal = [0.0, correction_db]

    if verbose:
        print(f"Channel calibration complete: ccal={phaser.ccal}")

    return phaser.ccal


def gain_calibration(phaser, verbose=False, averages=4):
    """Estimate per-element gain trim factors and store them in phaser.gcal.

    One element at a time at full gain, the rest off, and compare peaks. The
    weakest element becomes the reference so every trim is <= 1.0 -- you can
    only attenuate the others down to it, never boost past full scale.
    """
    plot_data = []
    measurements = []
    max_gain = 127

    for elem in range(8):
        for chan in range(8):
            phaser.set_chan_gain(chan, max_gain if chan == elem else 0, apply_cal=False)
            phaser.set_chan_phase(chan, 0, apply_cal=False)

        peak, spectrum = _capture_peak_dbfs(phaser, averages=averages)
        measurements.append(max(peak, -200.0))
        plot_data.append(np.asarray(spectrum if spectrum is not None else np.zeros(1)))

        if verbose:
            print(f"Gain calibration element {elem + 1}: peak={peak:.2f} dBFS")

    linear = [10 ** (m / 20) for m in measurements]
    ref = min([x for x in linear if x > 0] or [1.0])
    phaser.gcal = [min(1.0, ref / max(x, 1e-15)) for x in linear]

    if verbose:
        print(f"Gain calibration complete: gcal={phaser.gcal}")

    for chan in range(8):
        phaser.set_chan_gain(chan, max_gain, apply_cal=False)
        phaser.set_chan_phase(chan, 0, apply_cal=False)

    return plot_data


def phase_calibration(phaser, verbose=False, averages=2):
    """Estimate per-element phase offsets by sweeping adjacent channel pairs.

    Walks pairs (0,1), (1,2) ... (6,7): hold the left element at its already
    solved offset, sweep the right one, keep the phase that peaks the summed
    response. Offsets therefore accumulate along the array from element 0.
    """
    phase_values = np.arange(-180, 180 + phaser.phase_step_size / 2, phaser.phase_step_size)
    plot_data = []
    max_gain = 127
    phaser.pcal = [0.0] * 8

    for pair_idx in range(7):
        response = []
        for chan in range(8):
            phaser.set_chan_gain(chan, 0, apply_cal=False)
            phaser.set_chan_phase(chan, 0, apply_cal=False)

        phaser.set_chan_gain(pair_idx, max_gain, apply_cal=False)
        phaser.set_chan_gain(pair_idx + 1, max_gain, apply_cal=False)
        phaser.set_chan_phase(pair_idx, phaser.pcal[pair_idx], apply_cal=False)

        for candidate in phase_values:
            phaser.set_chan_phase(pair_idx + 1, candidate, apply_cal=False)
            peak, _ = _capture_peak_dbfs(phaser, averages=averages)
            response.append(peak)

        best_idx = int(np.argmax(response))
        phaser.pcal[pair_idx + 1] = float(phase_values[best_idx])
        plot_data.append(np.asarray(response))

        if verbose:
            print(
                f"Phase calibration pair {pair_idx + 1}->{pair_idx + 2}: "
                f"best={phaser.pcal[pair_idx + 1]:.4f} deg"
            )

    for chan in range(8):
        phaser.set_chan_gain(chan, max_gain, apply_cal=False)
        phaser.set_chan_phase(chan, phaser.pcal[chan], apply_cal=False)

    return phase_values, plot_data

