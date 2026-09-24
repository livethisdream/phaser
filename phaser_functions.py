import json
import os
import pickle
import tempfile
import time

import numpy as np


REPO_DIR = os.path.dirname(__file__)


def _repo_path(filename):
    return os.path.join(REPO_DIR, filename)


CALIBRATION_JSON = "calibration.json"

# Legacy stores, still READ so a Pi calibrated before this change keeps working.
# Each is superseded the next time that particular calibration is re-run.
LEGACY_PICKLES = {
    "phase_cal": "phase_cal_val.pkl",
    "gain_cal": "gain_cal_val.pkl",
    "channel_cal": "channel_cal_val.pkl",
}
LEGACY_HB100_TXT = "hb100_cal.txt"


def load_calibration():
    """The whole calibration store as a dict, or {} if there is not one yet.

    A malformed file returns {} rather than raising: callers fall back to the
    legacy stores and then to defaults, and a corrupt calibration should not
    stop the backend from starting.
    """
    try:
        with open(_repo_path(CALIBRATION_JSON), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_calibration(**updates):
    """Merge `updates` into the store and write it atomically.

    Merge, not replace: calibrations are run individually, so writing phase
    must not discard gain. Atomic because the alternative -- a truncated JSON
    file after a power cut mid-workshop -- loses every value at once, which is
    strictly worse than the per-file stores this replaces.
    """
    payload = load_calibration()
    payload.update(updates)
    payload["version"] = 1
    payload["updated_at"] = time.time()

    target = _repo_path(CALIBRATION_JSON)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target) or ".",
                               prefix=".calibration-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return payload


def _coerce_list(values, default, length, cast=float):
    """Exactly `length` values of type `cast`, whatever nonsense came in."""
    try:
        out = [cast(v) for v in list(values)[:length]]
    except (TypeError, ValueError):
        return list(default)
    return (out + list(default))[:length]


def load_cal_values(key, default, length):
    """One calibration array: JSON first, then the legacy pickle, then default.

    Per-key rather than all-or-nothing, so a Pi that has re-run only its phase
    calibration reads phase from JSON and gain from the old pickle, instead of
    silently reverting phase to defaults.
    """
    stored = load_calibration().get(key)
    if stored is not None:
        return _coerce_list(stored, default, length)

    legacy = LEGACY_PICKLES.get(key)
    if legacy:
        # A file this code wrote, on this Pi. Still: pickle executes on load,
        # which is one of the reasons the store is moving to JSON.
        values = _load_pickle_file(legacy, None)
        if values is not None:
            return _coerce_list(values, default, length)
    return list(default)


def save_phase_cal(values):
    """Persist per-element phase offsets (degrees)."""
    values = _coerce_list(values, [0.0] * 8, 8)
    save_calibration(phase_cal=values)
    return values


def save_gain_cal(values):
    """Persist per-element gain trims (0..1)."""
    values = _coerce_list(values, [1.0] * 8, 8)
    save_calibration(gain_cal=values)
    return values


def save_channel_cal(values):
    """Persist the two-channel SDR gain correction (dB)."""
    values = _coerce_list(values, [0.0] * 2, 2)
    save_calibration(channel_cal=values)
    return values


def _load_pickle_file(filename, default_value):
    path = _repo_path(filename)
    try:
        with open(path, "rb") as file:
            return pickle.load(file)
    except FileNotFoundError:
        return default_value


def load_hb100_cal():
    """HB100 frequency in Hz. JSON first, then the legacy hb100_cal.txt."""
    stored = load_calibration().get("hb100_freq_hz")
    if stored is not None:
        try:
            return float(stored)
        except (TypeError, ValueError):
            pass

    cal_file = _repo_path(LEGACY_HB100_TXT)
    if os.path.exists(cal_file):
        with open(cal_file, "r", encoding="utf-8") as f:
            try:
                return float(f.read().strip())
            except ValueError:
                pass
    raise FileNotFoundError("Calibration file not found or invalid.")


def save_hb100_cal(freq_hz):
    """Persist the HB100 frequency. Writes JSON only; the old text file is
    still read, so an existing Pi keeps working until this runs once."""
    value = float(freq_hz)
    save_calibration(hb100_freq_hz=value)
    return value


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



# --- Signal validation ------------------------------------------------------
#
# Both the HB100 search and the calibration used to trust whichever bin held
# the most energy. That is only safe when the loudest thing in the band is the
# signal, and on this hardware it often is not: the Pluto emits a spur at a
# fixed BASEBAND offset which measured -20 dBFS on a kit whose HB100 sat 28 MHz
# away from the frequency the stored cal claimed.
#
# The search steps the LO and maps each capture to absolute frequency, so a
# fixed-baseband spur lands on a DIFFERENT absolute frequency at every step --
# one false candidate per step, and the loudest of them wins the argmax. The
# scan then reports a frequency the array cannot receive; the phase
# calibration, tuned 28 MHz off the tone, maximises a quantity that does not
# depend on element phase at all; and the pcal it writes is noise. Reruns on
# the same bench disagreed by 256 degrees and channel_cal came out negative,
# which is impossible for a magnitude ratio. Nothing in the pipeline noticed.
#
# What separates the two is movement. A real tone holds its ABSOLUTE frequency
# while the LO moves; a spur holds its BASEBAND offset. Everything below rests
# on that one distinction.

# A real HB100 at workshop range clears the floor by tens of dB; 10 dB is a
# floor for "there is something here", not a target.
MIN_SCAN_SNR_DB = 10.0

# Measured ~15 dB on a healthy bench and 0.0 dB with no illumination, so 6 dB
# is comfortably clear of both.
MIN_ARRAY_CONTRAST_DB = 6.0

# Below this many LO steps the per-bin median is estimated from too few
# samples to be a trustworthy spur template, so rejection is skipped rather
# than applied badly.
SCAN_MIN_STEPS_FOR_SPUR_REJECTION = 8


def reject_fixed_baseband_spurs(matrix):
    """Subtract, per baseband bin, the median across all LO steps.

    A spur occupies the same bin in every row, so that bin's median *is* the
    spur and subtracting it leaves nothing. A real tone occupies a given bin
    only in the few rows whose window covers it, so it barely moves that bin's
    median and survives the subtraction intact.

    `matrix` is (LO steps x baseband bins) in dB.
    """
    m = np.asarray(matrix, dtype=float)
    if m.ndim != 2:
        raise ValueError("expected a 2-D (steps x bins) matrix")
    return m - np.median(m, axis=0)


def find_scan_peak(matrix, baseband_freqs, step_freqs,
                   min_steps=SCAN_MIN_STEPS_FOR_SPUR_REJECTION):
    """Locate the strongest *real* tone in a stepped-LO scan.

    The caller owns any spectral inversion: pass amplitudes already in the
    same bin order as `baseband_freqs`.

    `raw_freq_hz` is what a plain argmax would have returned. It is reported
    rather than discarded so callers can log the disagreement -- that
    disagreement is the failure this function exists to catch.
    """
    m = np.asarray(matrix, dtype=float)
    bb = np.asarray(baseband_freqs, dtype=float)
    steps = np.asarray(step_freqs, dtype=float)
    if m.shape != (steps.size, bb.size):
        raise ValueError(
            "matrix %s does not match %d steps x %d bins"
            % (m.shape, steps.size, bb.size)
        )

    absolute = steps[:, None] + bb[None, :]
    raw_r, raw_c = np.unravel_index(int(np.argmax(m)), m.shape)

    spur_rejected = steps.size >= min_steps
    residual = reject_fixed_baseband_spurs(m) if spur_rejected else m

    r, c = np.unravel_index(int(np.argmax(residual)), residual.shape)
    freq = float(absolute[r, c])
    noise = float(np.median(residual))
    snr = float(residual[r, c]) - noise

    # A tone inside the swept band is visible from every step whose window
    # covers it. One that only a single step can see is either at the very
    # edge of the sweep or is not a tone.
    half_window = float(np.max(np.abs(bb))) if bb.size else 0.0
    confirming = 0
    for row in range(steps.size):
        offset = freq - steps[row]
        if abs(offset) > half_window:
            continue
        col = int(np.argmin(np.abs(bb - offset)))
        if residual[row, col] >= noise + 0.5 * max(snr, 0.0):
            confirming += 1

    return {
        "freq_hz": freq,
        "snr_db": snr,
        "peak_db": float(m[r, c]),
        "steps_confirming": confirming,
        "spur_rejected": bool(spur_rejected),
        "raw_freq_hz": float(absolute[raw_r, raw_c]),
    }


def scan_peak_is_trustworthy(result, min_snr_db=MIN_SCAN_SNR_DB,
                             min_confirming=1):
    """(ok, reason) for a `find_scan_peak` result.

    Returns the reason as text so the caller can put it in front of whoever is
    standing at the bench, which is the whole point: the old behaviour was to
    save a bogus frequency silently.

    `min_confirming` defaults to 1, i.e. off. It is tempting to require that
    several LO steps agree, on the grounds that a real tone is visible from
    every step whose window covers it -- but how many steps that is depends on
    the ANALOG bandwidth, not the sample rate. The HB100 search runs
    rx_rf_bandwidth at 10 MHz with a 20 MHz filter and steps 10 MHz, so usable
    coverage is about +/-5 MHz and a tone is normally seen by exactly one step.
    Requiring two rejected a real 72.9 dB tone on a healthy bench. Raise it
    only for a scan whose steps genuinely overlap.

    Spur rejection is what removes spurs here: a fixed-baseband spur subtracts
    to nothing, so it cannot clear `min_snr_db` afterwards. Callers should
    still confirm against a live LO shift, which tests the real-versus-spur
    distinction directly rather than by proxy.
    """
    if result["snr_db"] < min_snr_db:
        return False, (
            "peak is only %.1f dB above the noise floor (need %.1f dB) -- "
            "no signal detected" % (result["snr_db"], min_snr_db)
        )
    if result["steps_confirming"] < min_confirming:
        return False, (
            "peak was seen by only %d LO step(s), fewer than the %d required "
            "for this scan geometry"
            % (result["steps_confirming"], min_confirming)
        )
    return True, "ok"


def measure_array_contrast(phaser, averages=4, max_gain=127):
    """(contrast_db, on_dbfs, off_dbfs) for all elements on versus all off.

    This is the one measurement that tells "the array is receiving a signal"
    apart from "something inside the receiver is loud". Element state cannot
    change a spur produced after the mixer, so a healthy bench gives a large
    positive number here and a bench with no illumination gives about zero.
    """
    for chan in range(8):
        phaser.set_chan_gain(chan, max_gain, apply_cal=False)
        phaser.set_chan_phase(chan, 0, apply_cal=False)
    on, _ = _capture_peak_dbfs(phaser, averages=averages)

    for chan in range(8):
        phaser.set_chan_gain(chan, 0, apply_cal=False)
    off, _ = _capture_peak_dbfs(phaser, averages=averages)

    # Leave the array as we found it; callers calibrate straight after.
    for chan in range(8):
        phaser.set_chan_gain(chan, max_gain, apply_cal=False)

    return float(on - off), float(on), float(off)


# --- LO planning ------------------------------------------------------------
#
# LO = SignalFreq + Rx_freq, and the ADF4159 register takes a QUARTER of it
# because the CN0566 divides by 4 ahead of the PLL's RFIN. Both halves have
# already produced a silent failure on this hardware:
#
#   - writing the LO undivided (c9761c8) asked for four times the frequency,
#     which the part accepted and read straight back while receiving nothing;
#   - asking for an LO the chain cannot reach does exactly the same thing.
#
# There is no lock-detect to consult. MUXOUT is not configured as one on this
# board -- it reads 0 at every LO, including one demonstrably receiving a
# 43 dB pattern -- so a bad LO is indistinguishable from a dead source until
# someone measures. Hence arithmetic, checked before the kit reaches a table.
#
# Measured 2026-09-24 on kit "phaser" with the HB100 at 10.446953 GHz, by
# stepping Rx_freq and reading the peak out of a 30 MSPS capture:
#
#     Rx_freq   LO (GHz)   ADF4159 (GHz)   peak dBFS   SNR
#      2.15      12.597       3.1492         -4.12    63.9   clean
#      2.20      12.647       3.1617         -4.85    64.2   clean
#      2.35      12.797       3.1992         -4.86    64.1   clean
#      2.40      12.847       3.2117        -14.08    77.3   degrading
#      2.45      12.897       3.2242        -74.77    19.2   dead
#      2.50      12.947       3.2367        -74.65    19.1   dead
#
# The readback matched the request at every row, including the dead ones.
#
# One kit, one HB100. Treat the ceiling as "measured here", not as a datasheet
# limit -- which is why crossing it warns rather than fails.

# Thresholds sit just below the measurements they classify, so the measured
# rows land on the right side of them: 12.797 was the last clean reading and
# 12.897 the first dead one, so anything at or above 12.89 is "measured dead"
# and 12.80 upward is "past what was measured clean".
LO_USABLE_CEILING_HZ = 12.80e9
LO_DEAD_HZ = 12.89e9

# What an HB100 can be. Not a tolerance -- the 2025 workshop labs say outright
# that it "is not well controlled - it could be anywhere from 10.1 to 10.7 GHz".
HB100_MIN_HZ = 10.1e9
HB100_MAX_HZ = 10.7e9


def lo_chain(signal_freq_hz, rx_freq_hz):
    """The LO and ADF4159 register for a signal and IF, in Hz."""
    lo = float(signal_freq_hz) + float(rx_freq_hz)
    return {"lo_hz": lo, "adf4159_hz": lo / 4.0}


def lo_warnings(signal_freq_hz, rx_freq_hz,
                hb100_max_hz=HB100_MAX_HZ,
                ceiling_hz=LO_USABLE_CEILING_HZ,
                dead_hz=LO_DEAD_HZ):
    """Everything wrong with this IF choice, worst first, as plain sentences.

    Two separate questions, and the second is the one that bites a workshop:
    whether THIS kit's HB100 is reachable, and whether the IF can reach every
    HB100 the labs say a kit might be handed. An IF that works on the bench and
    fails on a unit 200 MHz higher is a configuration that passes its own test
    and then goes deaf at the table.
    """
    out = []
    lo = lo_chain(signal_freq_hz, rx_freq_hz)["lo_hz"]

    if lo >= dead_hz:
        out.append(
            "LO %.3f GHz is at or past %.2f GHz, where this chain was measured "
            "receiving nothing. The write will be accepted and read back "
            "correctly; the only symptom is an empty spectrum."
            % (lo / 1e9, dead_hz / 1e9)
        )
    elif lo > ceiling_hz:
        out.append(
            "LO %.3f GHz is above the highest frequency measured receiving "
            "cleanly (%.2f GHz). It may work; it was not measured to."
            % (lo / 1e9, ceiling_hz / 1e9)
        )

    worst_lo = float(hb100_max_hz) + float(rx_freq_hz)
    if worst_lo > ceiling_hz:
        reachable = ceiling_hz - float(rx_freq_hz)
        out.append(
            "with Rx_freq %.2f GHz this kit cannot cover the whole HB100 range: "
            "a unit above %.3f GHz needs an LO past %.2f GHz. The labs put HB100 "
            "anywhere in %.1f-%.1f GHz, so a swapped source can go deaf with no "
            "configuration change."
            % (float(rx_freq_hz) / 1e9, reachable / 1e9, ceiling_hz / 1e9,
               HB100_MIN_HZ / 1e9, hb100_max_hz / 1e9)
        )

    return out
