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
