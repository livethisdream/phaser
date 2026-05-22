import os
import json
import pickle
import time

import numpy as np


REPO_DIR = os.path.dirname(__file__)
CALIBRATION_JSON = "calibration.json"


def _repo_path(filename):
    return os.path.join(REPO_DIR, filename)


def _load_pickle_file(filename, default_value):
    path = _repo_path(filename)
    try:
        with open(path, "rb") as file:
            return pickle.load(file)
    except FileNotFoundError:
        return default_value


def _load_json_file(filename, default_value):
    path = _repo_path(filename)
    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
            if isinstance(data, dict):
                return data
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return dict(default_value)


def _save_json_file(filename, data):
    path = _repo_path(filename)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, sort_keys=True)


def _coerce_list(values, default, length, cast=float):
    out = []
    for value in list(values):
        try:
            out.append(cast(value))
        except Exception:
            continue
    if len(out) < length:
        out += list(default)[len(out):length]
    return out[:length]


def _load_calibration_json():
    return _load_json_file(CALIBRATION_JSON, {})


def _save_calibration_json(updates):
    payload = _load_calibration_json()
    payload.update(updates)
    payload["version"] = 1
    payload["updated_at"] = time.time()
    _save_json_file(CALIBRATION_JSON, payload)


def load_hb100_cal():
    """Load HB100 calibration signal frequency if it exists."""
    payload = _load_calibration_json()
    freq = payload.get("hb100_freq_hz")
    if freq is not None:
        try:
            return float(freq)
        except Exception:
            pass

    # Legacy fallback kept for compatibility with older scripts/installations.
    cal_file = _repo_path("hb100_cal.txt")
    if os.path.exists(cal_file):
        with open(cal_file, "r", encoding="utf-8") as f:
            try:
                freq = float(f.read().strip())
                return freq
            except ValueError:
                pass
    raise FileNotFoundError("Calibration file not found or invalid.")


def save_hb100_cal(freq_hz):
    """Persist the detected HB100 frequency for future server runs."""
    freq_hz = float(freq_hz)
    _save_calibration_json({"hb100_freq_hz": freq_hz})


def save_phase_cal(values):
    values = _coerce_list(values, [0.0] * 8, 8, float)
    _save_calibration_json({"phase_cal": values})
    return values


def save_gain_cal(values):
    values = _coerce_list(values, [1.0] * 8, 8, float)
    _save_calibration_json({"gain_cal": values})
    return values


def save_channel_cal(values):
    values = _coerce_list(values, [0.0] * 2, 2, float)
    _save_calibration_json({"channel_cal": values})
    return values


def load_phase_cal(default=None, filename="phase_cal_val.pkl"):
    if default is None:
        default = [0.0] * 8
    payload = _load_calibration_json()
    if isinstance(payload.get("phase_cal"), list):
        return _coerce_list(payload.get("phase_cal"), default, 8, float)
    values = list(_load_pickle_file(filename, default))
    return _coerce_list(values, default, 8, float)


def load_gain_cal(default=None, filename="gain_cal_val.pkl"):
    if default is None:
        default = [1.0] * 8
    payload = _load_calibration_json()
    if isinstance(payload.get("gain_cal"), list):
        return _coerce_list(payload.get("gain_cal"), default, 8, float)
    values = list(_load_pickle_file(filename, default))
    return _coerce_list(values, default, 8, float)


def load_channel_cal(default=None, filename="channel_cal_val.pkl"):
    if default is None:
        default = [0.0] * 2
    payload = _load_calibration_json()
    if isinstance(payload.get("channel_cal"), list):
        return _coerce_list(payload.get("channel_cal"), default, 2, float)
    values = list(_load_pickle_file(filename, default))
    return _coerce_list(values, default, 2, float)


def spec_est(data, sample_rate, ref=2**12, plot=False):
    """Simple spectrum estimate compatible with legacy HB100 scan usage."""
    samples = np.asarray(data)
    if samples.size == 0:
        return np.empty(0), np.empty(0)

    window = np.blackman(samples.size)
    spectrum = np.fft.fft(samples * window)
    magnitude = np.abs(spectrum) / max(np.sum(window), 1)
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


def _rx_channels(phaser):
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

    avg0 = max(float(np.mean(rms0)), 1e-15)
    avg1 = max(float(np.mean(rms1)), 1e-15)
    correction_db = 20 * np.log10(avg0 / avg1)
    phaser.ccal = [0.0, correction_db]

    if verbose:
        print(f"Channel calibration complete: ccal={phaser.ccal}")

    return phaser.ccal


def gain_calibration(phaser, verbose=False, averages=4):
    """Estimate per-element gain trim factors and store them in phaser.gcal."""
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
    """Estimate per-element phase offsets by sweeping adjacent channel pairs."""
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


def calculate_plot(phases_deg, gains=None):
    """Simple array-factor helper retained for compatibility with legacy scripts."""
    phases = np.deg2rad(np.asarray(phases_deg, dtype=float))
    if gains is None:
        gains = np.ones_like(phases)
    gains = np.asarray(gains, dtype=float)
    angles = np.linspace(-90, 90, 181)
    steering = np.deg2rad(angles)
    array_factor = []
    for theta in steering:
        weights = gains * np.exp(1j * (np.arange(phases.size) * np.pi * np.sin(theta) + phases))
        array_factor.append(np.abs(np.sum(weights)))
    return angles, np.asarray(array_factor)

