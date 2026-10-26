import json
import math
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


def _flush_rx(sdr):
    """Drop whatever was queued before the last retune.

    Measured on this hardware (PR #29): without this a capture returns the
    PREVIOUS tuning's data at any settle time, and discarding one or two reads
    does not help -- which silently labels every sweep step with the next
    step's frequency. Falls back to a discarded read for a receiver that has
    no rx_destroy_buffer, which is what the fakes in the tests exercise.
    """
    try:
        sdr.rx_destroy_buffer()
        return
    except AttributeError:
        pass
    except Exception:
        return
    try:
        sdr.rx()
    except Exception:
        pass


def _capture_peak_dbfs(phaser, averages=4):
    """Mean peak of the summed-channel spectrum over `averages` captures.

    Flushed first, because every caller reads this immediately after changing
    the array -- a gain, a phase, an element switched off -- and on this
    hardware a capture otherwise returns the state BEFORE the change (PR #29
    measured that at any settle time, and with one or two discarded reads).

    Measured consequences on phaser-00, so nobody re-litigates this:
    measure_array_contrast is unaffected either way (32.3-33.4 dB flushed and
    unflushed -- it separates two states ~33 dB apart and a one-capture lag
    cannot close that). Phase calibration's run-to-run scatter is ALSO
    unchanged by it: five runs, up to 104 degrees apart on element 8, flushed
    and unflushed alike. That scatter is the argmax in phase_calibration, not
    staleness -- see the note there. The flush stays because reading the state
    you just set is correct regardless, and it costs one buffer.
    """
    _flush_rx(phaser.sdr)
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


def _fit_pair_phase(phase_values_deg, response_db):
    """The phase offset that peaks a two-element sum, from the WHOLE curve.

    Replaces an argmax, which is the wrong estimator for this shape. Two
    elements summing give a response proportional to |1 + exp(j*delta)|, so
    the peak is quadratic: 20 degrees off costs 0.26 dB. A couple of tenths
    of a dB of noise therefore moves an argmax by tens of degrees, and
    phase_calibration feeds each pair's answer into the next, so that error
    accumulates along the array. Measured on phaser-00: five runs, element 8
    spanning 104 degrees, 27.5 degrees of scatter averaged over elements 2-8.

    The curve is a known one-parameter family, so fit it. Every sample then
    contributes, including the steep flanks and the null, which is where the
    phase information actually is -- the peak is the least informative part
    of the sweep.

    The fit is the first Fourier coefficient: with f(phi) = g(phi - phi0) and
    g non-negative, peaked at zero and having a positive first coefficient,
    sum(f * exp(j*phi)) = exp(j*phi0) * (positive real), so its angle is
    phi0. That holds whether the response is read as amplitude or as power --
    both give a positive coefficient -- and it is immune to an additive
    pedestal, because a constant is orthogonal to exp(j*phi). That last
    property matters here: the measured peak never falls to zero, it falls to
    the noise floor, and an estimator that cared would have to know where the
    floor was.

    Returns (offset_deg in [-180, 180), quality), where quality is
    |c1| / |c0| -- about 0.33 for a clean two-element response and near zero
    for a flat one. It is the only warning available that a pair's sweep
    carried no phase information at all.
    """
    phi_deg = np.asarray(phase_values_deg, dtype=float)
    response = np.asarray(response_db, dtype=float)
    if phi_deg.size == 0:
        return 0.0, 0.0

    # The sweep is generated inclusive of both ends, so -180 and +180 are the
    # same electrical phase measured twice. Harmless to an argmax, a small
    # double-weighted term here.
    if phi_deg.size > 1 and abs((phi_deg[-1] - phi_deg[0]) - 360.0) < 1e-6:
        phi_deg = phi_deg[:-1]
        response = response[:-1]

    phi = np.radians(phi_deg)
    linear = 10.0 ** (response / 20.0)
    c1 = np.sum(linear * np.exp(1j * phi))
    c0 = np.sum(linear)

    offset = float(np.degrees(np.angle(c1)))
    if offset >= 180.0:
        offset -= 360.0
    quality = float(np.abs(c1) / c0) if c0 > 0 else 0.0
    return offset, quality


def phase_calibration(phaser, verbose=False, averages=2):
    """Estimate per-element phase offsets by sweeping adjacent channel pairs.

    Walks pairs (0,1), (1,2) ... (6,7): hold the left element at its already
    solved offset, sweep the right one, keep the phase that peaks the summed
    response. Offsets therefore accumulate along the array from element 0.

    KNOWN: this does not repeat. Five runs on phaser-00 (2026-10-15), on a
    healthy bench at 85 dB SNR and 33 dB array contrast, returned element 8
    offsets spanning 104 degrees: +8.4, +30.9, +50.6, +95.6, +112.5.

    The cause is the estimator, not the measurement. Two elements summing give
    P(phi) proportional to 2(1 + cos(phi - phi0)), whose peak is quadratic: 20
    degrees off the true offset costs 0.26 dB. A couple of tenths of a dB of
    noise therefore moves the argmax by tens of degrees, and since each pair
    seeds the next, it accumulates along the array -- which is the shape
    observed, element 2 scattering ~25 degrees and element 8 ~104.

    Flushing the capture does not help; that was measured separately and the
    scatter was identical. What would is using the whole curve instead of its
    flattest point, e.g. phi0 = angle(sum(P_linear * exp(j*phi))), the first
    Fourier coefficient of a known one-parameter family. Left alone here
    because changing a calibration algorithm is a decision, not a cleanup.
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

        offset, quality = _fit_pair_phase(phase_values, response)
        phaser.pcal[pair_idx + 1] = offset
        plot_data.append(np.asarray(response))

        if verbose:
            # The argmax is printed alongside because it is what this used to
            # return; the gap between them is the error being removed, and it
            # is worth being able to see it on a real bench.
            argmax_deg = float(phase_values[int(np.argmax(response))])
            flag = "" if quality >= MIN_PHASE_FIT_QUALITY else "  <- near-flat response"
            print(
                f"Phase calibration pair {pair_idx + 1}->{pair_idx + 2}: "
                f"fit={offset:8.3f} deg  (argmax={argmax_deg:8.3f}, "
                f"quality={quality:.3f}){flag}"
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

# A clean two-element sweep fits with |c1|/|c0| near 0.33 (the exact value for
# a rectified-cosine response). Flat noise gives ~0. A third of the ideal is
# comfortably clear of both, and only reports -- a pair that fits badly still
# produces the best estimate available, and refusing to calibrate on it would
# be worse than calibrating with a warning.
MIN_PHASE_FIT_QUALITY = 0.11

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
    standing at the bench, which is the whole point: the old behavior was to
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


# --- IF planning and per-unit characterization ------------------------------
#
# LO = SignalFreq + Rx_freq, and the ADF4159 register takes a QUARTER of it
# because the CN0566 divides by 4 ahead of the PLL's RFIN. Both halves have
# already produced a silent failure on this hardware:
#
#   - writing the LO undivided (c9761c8) asked for four times the frequency,
#     which the part accepted and read straight back while receiving nothing;
#   - asking for an IF this unit's chain cannot pass does exactly the same.
#
# There is no lock-detect to consult. MUXOUT is not configured as one on this
# board -- it reads 0 at every LO, including one demonstrably receiving a
# 43 dB pattern -- so a deaf configuration is indistinguishable from a dead
# source until someone measures.
#
# WHICH AXIS THE LIMIT IS ON
#
# An earlier sweep stepped Rx_freq, recorded where reception died, and called
# the result an LO ceiling. It could not have told the two apart: the source
# is fixed, so LO = SignalFreq + Rx_freq moves the LO and the IF together and
# only their difference identifies the cause.
#
# Measured on phaser-00, 2026-10-15, by parking the Pluto's rx_lo and moving
# ONLY the CN0566 LO -- which walks the tone across baseband instead of
# retuning the receiver, and so varies one axis at a time:
#
#     rx_lo parked   LO swept over      tone tracked   peak
#       2.0 GHz      12.398-12.408 GHz    11 of 11     -2.3 to -3.1 dBFS
#       2.2 GHz      12.598-12.608 GHz     0 of 11     -75 dBFS, noise only
#
# The ADF4159 sat at 3.14691 GHz receiving cleanly and at 3.14950 GHz
# receiving nothing -- 2.6 MHz apart, opposite outcomes, and the only other
# difference is rx_lo. The limit is on the IF, not the LO.
#
# Re-read on that axis, both kits measured so far have an IF ceiling:
#
#     kit          IF clean to   IF dead by   (LO at those IFs)
#     phaser          ~2.37         2.45       12.80 / 12.90 GHz
#     phaser-00        2.18         2.20       12.59 / 12.61 GHz
#
# About 200 MHz apart on nominally identical hardware, which is the whole
# argument for measuring the window per unit rather than hard-coding it: a
# ceiling taken off one kit is what made this bug possible. config.py ships
# Rx_freq = 2.2 GHz, which kit "phaser" could hear and phaser-00 could not.
#
# The LO was unconstrained everywhere a 10.4 GHz source can reach it -- 11.708
# to 12.608 GHz, flat at 85-88 dB SNR. A higher LO limit may well exist; this
# source cannot reach it, so nothing here claims one.

# Bootstrap only: the IF to use before a unit has been characterized. It sits
# ~480 MHz below the lower of the two ceilings measured so far, which is the
# entire basis for the number. It is a safe place to start a measurement from,
# not an answer -- phaser_characterize_headless.py replaces it with one.
DEFAULT_RX_FREQ_HZ = 1.7e9

# Used only when a unit has NOT been characterized: the lowest clean edge and
# the lowest dead point seen on any kit, so an unmeasured unit is judged
# against the worst case rather than the best.
IF_UNMEASURED_CEILING_HZ = 2.18e9
IF_UNMEASURED_DEAD_HZ = 2.20e9

# What an HB100 can be. Not a tolerance -- the 2025 workshop labs say outright
# that it "is not well controlled - it could be anywhere from 10.1 to 10.7 GHz".
HB100_MIN_HZ = 10.1e9
HB100_MAX_HZ = 10.7e9

# The sweep characterization runs. The range is part of the definition of the
# answer, because choose_rx_freq returns the middle of the clean run: widening
# or narrowing this moves the result. 1.2-2.6 GHz spans both kits measured so
# far with room either side, and 25 MHz places a ~15 MHz skirt within a step.
IF_SWEEP_START_HZ = 1.2e9
IF_SWEEP_STOP_HZ = 2.6e9
IF_SWEEP_STEP_HZ = 25e6

# A step is clean if it comes within this of the best step in the sweep. The
# skirt falls ~30 dB per 5 MHz, so every step on it lands far outside 6 dB and
# no threshold in this neighborhood changes which steps qualify.
IF_CLEAN_TOLERANCE_DB = 6.0

# Below this a "window" is measurement noise, not a window, and
# characterization fails rather than picking the middle of one.
IF_MIN_USABLE_SPAN_HZ = 100e6

# A sweep whose best step is below this is not believed at all. choose_rx_freq
# cannot reject a dead sweep on shape -- with nothing received, every step is
# within tolerance of every other and the clean run looks perfect -- so the
# absolute level is the only thing that gives it away. The whole array on a
# live HB100 reads ~85 dB on both kits measured; 30 dB is far below that and
# far above the ~15 dB a deaf configuration returns.
MIN_BOOTSTRAP_SNR_DB = 30.0

# Chosen IFs are rounded to this, purely so the number people read and type is
# a round one. Far finer than anything that changes reception.
IF_ROUNDING_HZ = 5e6


def lo_chain(signal_freq_hz, rx_freq_hz):
    """The LO and ADF4159 register for a signal and IF, in Hz."""
    lo = float(signal_freq_hz) + float(rx_freq_hz)
    return {"lo_hz": lo, "adf4159_hz": lo / 4.0}


def measure_if_window(phaser, sdr, signal_freq_hz,
                      start_hz=IF_SWEEP_START_HZ,
                      stop_hz=IF_SWEEP_STOP_HZ,
                      step_hz=IF_SWEEP_STEP_HZ,
                      averages=2, settle_s=0.2, progress=None):
    """Step the IF across `start_hz`..`stop_hz` and report what is received.

    Both halves of the tuning move together on every step, because they have
    to: the LO has to track the IF or the tone leaves the analysis window and
    every step reads as deaf whether it is or not.

    Returns a list of {"rx_freq_hz", "lo_hz", "peak_dbfs", "snr_db"}, in sweep
    order. `progress` is called with each row as it is measured, so a CLI can
    print a table while the sweep runs.
    """
    rows = []
    rx_freq = float(start_hz)
    while rx_freq <= float(stop_hz) + 1.0:
        chain = lo_chain(signal_freq_hz, rx_freq)
        _flush_rx(sdr)
        sdr.rx_lo = int(rx_freq)
        phaser.frequency = int(chain["adf4159_hz"])
        time.sleep(settle_s)
        _flush_rx(sdr)

        peak, spectrum = _capture_peak_dbfs(phaser, averages=averages)
        noise = float(np.median(spectrum)) if spectrum is not None else -200.0
        row = {
            "rx_freq_hz": float(rx_freq),
            "lo_hz": float(chain["lo_hz"]),
            "peak_dbfs": float(peak),
            "snr_db": float(peak - noise),
        }
        rows.append(row)
        if progress is not None:
            progress(row)
        rx_freq += float(step_hz)
    return rows


def choose_rx_freq(rows, tolerance_db=IF_CLEAN_TOLERANCE_DB,
                   min_span_hz=IF_MIN_USABLE_SPAN_HZ,
                   rounding_hz=IF_ROUNDING_HZ):
    """Pick this unit's IF from a sweep: the middle of its longest clean run.

    The middle, rather than a fixed back-off from the top edge, because the
    top edge is exactly what varies between units -- 200 MHz between the only
    two measured. Sitting in the middle of what this unit actually does is the
    one choice that does not depend on a number borrowed from another kit.

    Contiguity matters: a lone clean step the far side of a dead region is a
    measurement artefact, not part of the window, and averaging it in would
    drag the answer onto the skirt.

    Returns a dict with `ok` and, when ok, `rx_freq_hz` plus the window it
    came from. When not ok, `reason` says what to tell the operator.
    """
    usable = [r for r in rows if r.get("snr_db") is not None]
    if not usable:
        return {"ok": False, "reason": "the sweep produced no usable readings"}

    best = max(float(r["snr_db"]) for r in usable)
    cutoff = best - float(tolerance_db)

    # Longest contiguous run of clean steps, in sweep order.
    runs, current = [], []
    for row in rows:
        if float(row.get("snr_db", -1e9)) >= cutoff:
            current.append(row)
        else:
            if current:
                runs.append(current)
            current = []
    if current:
        runs.append(current)
    if not runs:
        return {"ok": False, "reason": "no step came within %.1f dB of the best"
                                       % tolerance_db}

    run = max(runs, key=len)
    lo_edge = float(run[0]["rx_freq_hz"])
    hi_edge = float(run[-1]["rx_freq_hz"])
    span = hi_edge - lo_edge
    window = {
        "clean_min_hz": lo_edge,
        "clean_max_hz": hi_edge,
        "span_hz": span,
        "best_snr_db": best,
        "clean_steps": len(run),
        "swept_steps": len(rows),
    }

    if span < float(min_span_hz):
        window.update({
            "ok": False,
            "reason": ("the clean run is only %.0f MHz wide (need %.0f MHz); "
                       "that is not a window, and usually means the HB100 was "
                       "not illuminating the array"
                       % (span / 1e6, float(min_span_hz) / 1e6)),
        })
        return window

    middle = (lo_edge + hi_edge) / 2.0
    chosen = math.floor(middle / rounding_hz + 0.5) * rounding_hz
    window.update({"ok": True, "rx_freq_hz": float(chosen)})
    return window


def save_if_cal(window, signal_freq_hz=None, rows=None):
    """Persist a characterization. Stored under its own key, so re-running it
    does not disturb the phase, gain or channel calibrations beside it."""
    payload = {
        "rx_freq_hz": float(window["rx_freq_hz"]),
        "clean_min_hz": float(window["clean_min_hz"]),
        "clean_max_hz": float(window["clean_max_hz"]),
        "best_snr_db": float(window.get("best_snr_db", 0.0)),
        "measured_at": time.time(),
    }
    if signal_freq_hz is not None:
        payload["hb100_freq_hz"] = float(signal_freq_hz)
    if rows:
        # Kept so a later argument about where the edge is can be settled from
        # the stored sweep instead of re-running it on a kit that has moved.
        payload["sweep"] = [
            {"rx_freq_hz": float(r["rx_freq_hz"]),
             "peak_dbfs": round(float(r["peak_dbfs"]), 2),
             "snr_db": round(float(r["snr_db"]), 1)}
            for r in rows
        ]
    save_calibration(if_window=payload)
    return payload


def load_if_cal():
    """The stored characterization, or None if this unit has never had one."""
    stored = load_calibration().get("if_window")
    if not isinstance(stored, dict):
        return None
    try:
        float(stored["rx_freq_hz"])
    except (KeyError, TypeError, ValueError):
        return None
    return stored


def rx_freq_source(config_module=None):
    """Which IF to run at, where it came from, and anything wrong with it.

    Precedence, measurement first. A measurement is the only value here taken
    from THIS unit; config.py is site-owned, never deployed over, and ships a
    value that is wrong on at least one kit in the field, so it does not get
    to override one. `Rx_freq_override` exists so a bench can still force a
    value deliberately -- it is a separate name precisely so that setting it
    is a decision rather than a leftover.

    Returns {"rx_freq_hz", "source", "warnings"}.
    """
    warnings = []
    configured = None
    override = None
    if config_module is not None:
        try:
            override = float(getattr(config_module, "Rx_freq_override", None) or 0) or None
        except (TypeError, ValueError):
            override = None
        try:
            configured = float(getattr(config_module, "Rx_freq", None) or 0) or None
        except (TypeError, ValueError):
            configured = None

    stored = load_if_cal()

    if override is not None:
        if stored is not None and abs(override - float(stored["rx_freq_hz"])) > 1e6:
            warnings.append(
                "config.Rx_freq_override = %.3f GHz is overriding this unit's "
                "measured %.3f GHz. Remove the override to go back to the "
                "measurement." % (override / 1e9, float(stored["rx_freq_hz"]) / 1e9)
            )
        return {"rx_freq_hz": override, "source": "config.Rx_freq_override",
                "warnings": warnings}

    if stored is not None:
        value = float(stored["rx_freq_hz"])
        if configured is not None and abs(configured - value) > 1e6:
            warnings.append(
                "config.Rx_freq = %.3f GHz disagrees with this unit's measured "
                "%.3f GHz; using the measurement. config.py is not deployed "
                "between kits, so its value is whatever this card happened to "
                "ship with." % (configured / 1e9, value / 1e9)
            )
        return {"rx_freq_hz": value, "source": "calibration.json (measured)",
                "warnings": warnings}

    if configured is not None:
        warnings.append(
            "this unit has not been characterized, so config.Rx_freq = %.3f "
            "GHz is being taken on trust. Run phaser_characterize_headless.py."
            % (configured / 1e9)
        )
        return {"rx_freq_hz": configured, "source": "config.py (unverified)",
                "warnings": warnings}

    warnings.append(
        "no measurement and no config.Rx_freq; falling back to the %.3f GHz "
        "bootstrap default. Run phaser_characterize_headless.py."
        % (DEFAULT_RX_FREQ_HZ / 1e9)
    )
    return {"rx_freq_hz": DEFAULT_RX_FREQ_HZ, "source": "built-in default",
            "warnings": warnings}


def resolve_rx_freq(config_module=None):
    """The IF to run at, in Hz. See rx_freq_source for where it comes from."""
    return rx_freq_source(config_module)["rx_freq_hz"]


def rf_chain_warnings(signal_freq_hz, rx_freq_hz, window=None):
    """Everything wrong with this IF on this unit, worst first, in sentences.

    Judged against this unit's measured window when there is one. Without one
    there is nothing to judge against but other kits, so the fallback uses the
    LOWEST edge anyone has measured -- an unmeasured unit should be told it is
    unverified, not reassured by another kit's headroom.
    """
    out = []
    rx = float(rx_freq_hz)
    if window is None:
        window = load_if_cal()

    if window is not None:
        clean_min = float(window["clean_min_hz"])
        clean_max = float(window["clean_max_hz"])
        if rx > clean_max or rx < clean_min:
            out.append(
                "IF %.3f GHz is outside this unit's measured window "
                "(%.3f-%.3f GHz). Outside it the write is still accepted and "
                "reads back correctly; the only symptom is an empty spectrum."
                % (rx / 1e9, clean_min / 1e9, clean_max / 1e9)
            )
        else:
            edge = min(rx - clean_min, clean_max - rx)
            if edge < 100e6:
                out.append(
                    "IF %.3f GHz is only %.0f MHz from the edge of this unit's "
                    "measured window (%.3f-%.3f GHz)."
                    % (rx / 1e9, edge / 1e6, clean_min / 1e9, clean_max / 1e9)
                )
        return out

    if rx >= IF_UNMEASURED_DEAD_HZ:
        out.append(
            "IF %.3f GHz is at or above %.2f GHz, where a kit in this fleet "
            "was measured receiving nothing, and this unit has never been "
            "characterized. Run phaser_characterize_headless.py."
            % (rx / 1e9, IF_UNMEASURED_DEAD_HZ / 1e9)
        )
    elif rx > IF_UNMEASURED_CEILING_HZ:
        out.append(
            "IF %.3f GHz is above the highest a kit in this fleet was measured "
            "receiving cleanly (%.2f GHz), and this unit has never been "
            "characterized. It may work; it was not measured to."
            % (rx / 1e9, IF_UNMEASURED_CEILING_HZ / 1e9)
        )
    else:
        out.append(
            "this unit has never been characterized; %.3f GHz is inside what "
            "other kits manage, which is not the same as inside what this one "
            "does. Run phaser_characterize_headless.py."
            % (rx / 1e9)
        )
    return out
