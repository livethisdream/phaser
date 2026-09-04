"""Radar signal processing and configuration — pure numpy, no hardware.

Everything in here is a function of its arguments. Nothing imports `adi`,
touches libiio, or reads the headless object's state. That is the point: the
hardware-poking half of the radar lives in phaser_cw_radar.py, which cannot be
imported on a machine without pyadi-iio, and so the DSP that ships to the Pi
had no test coverage anywhere pyadi-iio was absent -- tests/test_cw_radar.py
skipped itself on `No module named 'adi'` and the suite went green without
running a single radar assertion.

Splitting the math out fixes that, and gives the FMCW work (range, range-
Doppler, MTI, CFAR) somewhere to land that stays testable.

phaser_cw_radar re-exports the names below, so `from phaser_cw_radar import
process_cw_frame, DEFAULTS` keeps working for anything that already does it.
"""

import numpy as np


# Physical constants
C = 299_792_458.0


# Defaults lifted from radar/CW_RADAR_Waterfall.py
DEFAULTS = {
    "sample_rate": 600_000,
    "fft_size": 1024 * 64,
    "signal_freq": 100_000,
    "output_freq": 12.2e9,
    "center_freq": 2.2e9,
    "rx_gain": 30,
    "tx_gain": 0,
    "fft_window": "blackman",
    "taper": "blackman",  # Blackman ADAR1000 taper
    "vel_max": 30.0,      # m/s; crops the Doppler window sent to the frontend
}


TAPER_PRESETS = {
    "rect":     [127, 127, 127, 127, 127, 127, 127, 127],
    "hann":     [12,  43,  77,  100, 100, 77,  43,  12],
    # Match radar/CW_RADAR_Waterfall.py exactly:
    "blackman": [8,   34,  84,  127, 127, 84,  34,  8],
}

# ADAR1000 rx_gain is a 7-bit field.
TAPER_MAX = 127


def resolve_taper(taper):
    """Turn a taper request into eight ADAR1000 gain codes.

    Accepts a preset name ("rect" / "hann" / "blackman"), an explicit
    sequence of eight values, or None. Anything unrecognised falls back to the
    Blackman preset rather than raising: a bad taper should cost you a
    sidelobe, not the whole radar mode.
    """
    if taper is None:
        return list(TAPER_PRESETS["blackman"])

    if isinstance(taper, str):
        return list(TAPER_PRESETS.get(taper.lower(), TAPER_PRESETS["blackman"]))

    try:
        values = [int(round(float(v))) for v in taper]
    except (TypeError, ValueError):
        return list(TAPER_PRESETS["blackman"])

    if not values:
        return list(TAPER_PRESETS["blackman"])

    # Pad short lists with zeros and clamp into the 7-bit field.
    values = (values + [0] * 8)[:8]
    return [max(0, min(TAPER_MAX, v)) for v in values]


def cw_lo_freq(cfg):
    """The mixer LO a CW capture needs, in Hz.

    The Pluto transmits `signal_freq` of baseband on a `center_freq` carrier.
    The LTC5548 on the Phaser mixes that up to the antenna, and mixes the echo
    back down again with the same LO. For the transmitted carrier to land on
    `output_freq` and the echo to return as a `signal_freq` tone at the Pluto's
    `center_freq` Rx LO, the mixer has to run at the sum:

        LO = output_freq + signal_freq + center_freq

    which is what every ADI phaser radar example writes (as `// 4`, because the
    CN0566 divides by four ahead of the PLL -- SDR_LO_init does that division,
    so hand it this undivided value).
    """
    return float(cfg["output_freq"]) + float(cfg["signal_freq"]) + float(cfg["center_freq"])


def build_iq_tone(fs, n, signal_freq, scale=2 ** 14):
    """Build a cyclic IQ tone at the requested IF frequency, snapped to bin.

    Snapping to an exact FFT bin is what makes the buffer cyclic: a tone at a
    non-integer number of cycles per buffer steps in phase every time the
    hardware wraps the buffer, which smears the transmitted line.
    """
    n = int(n)
    fc_bin = round(float(signal_freq) / float(fs) * n)
    fc_exact = fc_bin * float(fs) / n
    t = np.arange(n) / float(fs)
    i = np.cos(2 * np.pi * t * fc_exact) * scale
    q = np.sin(2 * np.pi * t * fc_exact) * scale
    return i + 1j * q


def window(name, n):
    """FFT window by name. Unknown names get Blackman, matching the default."""
    name = (name or "").lower()
    if name in ("hann", "hanning"):
        return np.hanning(n)
    if name == "hamming":
        return np.hamming(n)
    if name in ("rect", "none"):
        return np.ones(n)
    return np.blackman(n)


# Kept under the old private name so phaser_cw_radar._window still resolves.
_window = window


def process_cw_frame(iq, fs, signal_freq, output_freq, fft_window="blackman",
                     downsample_iq=512, vel_max=None):
    """Compute Doppler spectrum, velocity axis, and stats for one CW frame.

    The CW radar transmits at signal_freq above DC (the IF tone). A moving
    target reflects with a Doppler shift around that tone; we shift the spectrum
    so the IF appears at 0 Hz, then convert that residual frequency to velocity:

        v = c * f_doppler / (2 * f_carrier)

    where f_carrier is the actual transmitted carrier (== output_freq for the
    CN0566 CW path).

    `vel_max` crops the returned window to +/- that many m/s, so the frontend
    gets a few hundred bins instead of the full 64k. It is a parameter rather
    than a constant because the UI has a "Velocity Max" control: pinned at a
    hardcoded 30 m/s, asking the UI for a wider window just added empty margin
    to the plot, because the bins were never sent.
    """
    n = int(len(iq))
    if n == 0:
        raise ValueError("Empty IQ buffer")

    win = window(fft_window, n)
    sp = np.abs(np.fft.fft(iq * win))
    sp = np.fft.fftshift(sp)
    mag = np.maximum(sp / np.sum(win), 1e-15)
    spectrum_db = 20 * np.log10(mag / (2 ** 11))

    freq = np.fft.fftshift(np.fft.fftfreq(n, 1.0 / fs))

    # Doppler frequency = freq - signal_freq (IF tone moves to 0 after shift)
    doppler_hz = freq - signal_freq

    f_carrier = float(output_freq) if output_freq else 1.0
    velocity = C * doppler_hz / (2.0 * f_carrier)

    if vel_max is None:
        vel_max = DEFAULTS["vel_max"]
    vel_max = abs(float(vel_max))

    mask = np.abs(velocity) <= vel_max
    if not np.any(mask):  # safety: keep at least the central 1024 bins
        center = n // 2
        half = min(512, center)
        mask = np.zeros(n, dtype=bool)
        mask[center - half:center + half] = True

    velocity_out = velocity[mask]
    spectrum_out = spectrum_db[mask]

    peak_idx = int(np.argmax(spectrum_out))
    peak_velocity = float(velocity_out[peak_idx])
    peak_magnitude_db = float(spectrum_out[peak_idx])

    # Optional downsampled |IQ| for the time-domain tab — keep it small.
    iq_mag = None
    if downsample_iq and n > downsample_iq:
        step = max(1, n // downsample_iq)
        iq_mag = np.abs(iq[::step])[:downsample_iq]

    out = {
        "velocity_axis": velocity_out.astype(np.float32).tolist(),
        "spectrum_db": spectrum_out.astype(np.float32).tolist(),
        "peak_velocity": peak_velocity,
        "peak_magnitude_db": peak_magnitude_db,
        "n_samples": n,
        "sample_rate": float(fs),
        "signal_freq": float(signal_freq),
        "output_freq": float(output_freq),
    }
    if iq_mag is not None:
        out["iq_mag"] = iq_mag.astype(np.float32).tolist()
    return out


# ---------------------------------------------------------------------------
# FMCW radar
#
# Everything below serves the four FMCW labs in docs/2025_Phaser_labs_Python.pdf
# (pp. 29-38): beat-frequency range, the range waterfall, range-Doppler, MTI and
# CFAR. It is all pure numpy -- the ADF4159 ramp and the chirp-synced capture
# that feed it live in the hardware module.
#
# The chain is:
#     iq -> chirp_matrix -> [mti_filter] -> range_doppler_map
# with range_profile() as the single-chirp case the range lab uses, and
# ca_cfar() applied to either.
# ---------------------------------------------------------------------------


FMCW_DEFAULTS = {
    "sample_rate": 600_000,
    "chirp_bw": 500e6,      # B, the ramp's frequency sweep
    "ramp_time": 1e-3,      # T_s, seconds
    "num_chirps": 1,        # 1 = the range lab; >1 gives range-Doppler
    "pri": 1e-3,            # pulse repetition interval, seconds
    "signal_freq": 100_000, # IF tone, and so the nominal 0 m beat frequency
    "output_freq": 12.2e9,
    "center_freq": 2.2e9,
    "rx_gain": 30,
    "tx_gain": 0,
    "fft_window": "blackman",
    "taper": "blackman",
    "range_max": 20.0,      # m; crops the profile sent to the frontend
    "mti": "none",          # "none" | "2pulse" | "3pulse"
    "zero_range_freq": None,  # None -> signal_freq; the lab's 0 m calibration
}


MTI_MODES = ("none", "2pulse", "3pulse")


def fmcw_slope(chirp_bw, ramp_time):
    """Ramp slope S = B / T_s, in Hz per second."""
    ramp_time = float(ramp_time)
    if ramp_time <= 0:
        raise ValueError("ramp_time must be positive")
    return float(chirp_bw) / ramp_time


def beat_freq_to_range(f_beat, chirp_bw, ramp_time):
    """R = c * f_b / (2 * S).

    The labs quote 3.3 kHz of beat per metre at B = 500 MHz and T_s = 1 ms,
    which is this relation read the other way round: 2*S/c = 3336 Hz/m.

    (Step 22 of the same lab asks whether 1 m moved the peak by "about 6.7kHz",
    which is twice what its own background section derives. The formula is the
    unambiguous half of that pair, so it is what is implemented here.)
    """
    slope = fmcw_slope(chirp_bw, ramp_time)
    return C * np.asarray(f_beat, dtype=float) / (2.0 * slope)


def range_to_beat_freq(target_range, chirp_bw, ramp_time):
    """Inverse of beat_freq_to_range: f_b = 2 * S * R / c."""
    slope = fmcw_slope(chirp_bw, ramp_time)
    return 2.0 * slope * np.asarray(target_range, dtype=float) / C


def range_resolution(chirp_bw):
    """Two targets closer than c/(2B) merge into one. Independent of ramp time,
    which is why the lab's "change chirp_BW to 200e6" question is about dot
    size and nothing else."""
    return C / (2.0 * float(chirp_bw))


def max_unambiguous_range(sample_rate, chirp_bw, ramp_time):
    """Range at which the beat frequency reaches Nyquist and folds."""
    return float(beat_freq_to_range(float(sample_rate) / 2.0, chirp_bw, ramp_time))


def velocity_resolution(num_chirps, pri, output_freq):
    """dv = c / (2 * f_c * M * PRI).

    The lab's "change num_chirps to 32 or 128, what happens to the velocity
    resolution?" is asking about the M in this denominator.
    """
    dwell = float(num_chirps) * float(pri)
    if dwell <= 0:
        raise ValueError("num_chirps and pri must be positive")
    return C / (2.0 * float(output_freq) * dwell)


def max_unambiguous_velocity(pri, output_freq):
    """+/- c / (4 * f_c * PRI); beyond this the Doppler shift aliases."""
    return C / (4.0 * float(output_freq) * float(pri))


def chirp_matrix(iq, num_chirps, samples_per_chirp=None):
    """Fold a multi-chirp capture into the labs' N x M matrix.

    Rows are fast time (samples within one chirp, separated by the sampling
    period); columns are slow time (chirp to chirp, separated by the PRI). The
    2D FFT of that matrix is what turns into range vs velocity.

    Trailing samples that do not fill a whole chirp are dropped rather than
    zero-padded: a partial chirp is not a chirp, and padding it would put a
    spurious return at zero Doppler.
    """
    iq = np.asarray(iq)
    num_chirps = int(num_chirps)
    if num_chirps < 1:
        raise ValueError("num_chirps must be >= 1")

    if samples_per_chirp is None:
        samples_per_chirp = len(iq) // num_chirps
    samples_per_chirp = int(samples_per_chirp)
    if samples_per_chirp < 1:
        raise ValueError(
            f"{len(iq)} samples cannot be split into {num_chirps} chirps"
        )

    usable = samples_per_chirp * num_chirps
    return iq[:usable].reshape(num_chirps, samples_per_chirp).T


def mti_filter(matrix, mode="none"):
    """Pulse-cancel along slow time to suppress everything that is not moving.

    A stationary target returns a near-identical signal on consecutive chirps,
    so differencing them cancels it; a moving one has advanced in Doppler phase
    and survives. Two-pulse is one difference, three-pulse is two -- a deeper
    null at zero Doppler for a wider notch around it.

    Each stage costs one chirp, so a 2-pulse filter on M chirps returns M-1
    columns and a 3-pulse returns M-2. The caller has to use the returned
    width for the Doppler axis, not the width it asked for.
    """
    matrix = np.asarray(matrix)
    mode = (mode or "none").lower()
    if mode in ("none", "off", ""):
        return matrix
    if matrix.ndim != 2:
        raise ValueError("mti_filter needs a 2D chirp matrix")

    if mode == "2pulse":
        if matrix.shape[1] < 2:
            raise ValueError("2-pulse MTI needs at least 2 chirps")
        return matrix[:, 1:] - matrix[:, :-1]
    if mode == "3pulse":
        if matrix.shape[1] < 3:
            raise ValueError("3-pulse MTI needs at least 3 chirps")
        return matrix[:, 2:] - 2.0 * matrix[:, 1:-1] + matrix[:, :-2]

    raise ValueError(f"unknown MTI mode {mode!r}; expected one of {MTI_MODES}")


def _range_bins(n, sample_rate, chirp_bw, ramp_time, zero_range_freq):
    """Positive-frequency half of an n-point FFT, mapped to range.

    Only the positive half carries range: a target's beat sits at
    (zero_range_freq + f_b), and zero_range_freq is the lab's crude 0 m
    calibration -- the tone does not land exactly on signal_freq, so the lab
    has you hold a target at 0 m and read off where the peak actually is.
    """
    freq = np.fft.fftfreq(n, 1.0 / float(sample_rate))[: n // 2]
    return beat_freq_to_range(freq - float(zero_range_freq), chirp_bw, ramp_time)


def _to_db(mag, win_sum):
    return 20.0 * np.log10(np.maximum(mag / win_sum, 1e-15) / (2 ** 11))


def range_profile(iq, sample_rate, chirp_bw, ramp_time, zero_range_freq=0.0,
                  fft_window="blackman", range_max=None, range_min=0.0):
    """One chirp -> one range profile. The FMCW range lab's main plot.

    Returns the range axis in metres alongside the magnitude in dB, cropped to
    [range_min, range_max] so the frontend gets a few hundred bins rather than
    the whole positive half.
    """
    iq = np.asarray(iq)
    n = int(len(iq))
    if n < 2:
        raise ValueError("need at least 2 samples for a range profile")

    win = window(fft_window, n)
    sp = np.abs(np.fft.fft(iq * win))[: n // 2]
    profile_db = _to_db(sp, np.sum(win))

    ranges = _range_bins(n, sample_rate, chirp_bw, ramp_time, zero_range_freq)

    mask = ranges >= float(range_min)
    if range_max is not None:
        mask &= ranges <= float(range_max)
    if not np.any(mask):
        mask = np.ones(len(ranges), dtype=bool)

    ranges_out = ranges[mask]
    profile_out = profile_db[mask]

    peak_idx = int(np.argmax(profile_out))
    return {
        "range_axis": ranges_out.astype(np.float32).tolist(),
        "profile_db": profile_out.astype(np.float32).tolist(),
        "peak_range": float(ranges_out[peak_idx]),
        "peak_magnitude_db": float(profile_out[peak_idx]),
        "range_resolution": float(range_resolution(chirp_bw)),
        "max_range": float(max_unambiguous_range(sample_rate, chirp_bw, ramp_time)),
        "n_samples": n,
    }


def range_doppler_map(matrix, sample_rate, chirp_bw, ramp_time, pri, output_freq,
                      zero_range_freq=0.0, fft_window="blackman",
                      range_max=None, range_min=0.0, vel_max=None):
    """2D FFT of the chirp matrix -> range vs velocity.

    Range comes from the fast-time FFT down each column, velocity from the
    slow-time FFT across each row. Returned `map_db` is indexed
    [range][velocity] so a frontend heatmap can use it directly with
    `range_axis` as y and `velocity_axis` as x.

    The Doppler axis is derived from the matrix's own column count, not from
    any requested num_chirps -- MTI has usually eaten one or two columns by
    the time this runs.
    """
    matrix = np.asarray(matrix)
    if matrix.ndim != 2:
        raise ValueError("range_doppler_map needs a 2D chirp matrix")
    n_fast, n_slow = matrix.shape
    if n_fast < 2 or n_slow < 2:
        raise ValueError(f"matrix too small for a 2D FFT: {matrix.shape}")

    win_fast = window(fft_window, n_fast)[:, None]
    win_slow = window(fft_window, n_slow)[None, :]

    spec = np.fft.fft(matrix * win_fast, axis=0)[: n_fast // 2, :]
    spec = np.fft.fft(spec * win_slow, axis=1)
    spec = np.fft.fftshift(spec, axes=1)

    map_db = _to_db(np.abs(spec), np.sum(win_fast) * np.sum(win_slow) / n_slow)

    ranges = _range_bins(n_fast, sample_rate, chirp_bw, ramp_time, zero_range_freq)
    doppler_hz = np.fft.fftshift(np.fft.fftfreq(n_slow, float(pri)))
    velocity = C * doppler_hz / (2.0 * float(output_freq))

    rmask = ranges >= float(range_min)
    if range_max is not None:
        rmask &= ranges <= float(range_max)
    if not np.any(rmask):
        rmask = np.ones(len(ranges), dtype=bool)

    vmask = np.ones(len(velocity), dtype=bool)
    if vel_max is not None:
        vmask = np.abs(velocity) <= abs(float(vel_max))
        if not np.any(vmask):
            vmask = np.ones(len(velocity), dtype=bool)

    ranges_out = ranges[rmask]
    velocity_out = velocity[vmask]
    map_out = map_db[np.ix_(rmask, vmask)]

    peak_r, peak_v = np.unravel_index(int(np.argmax(map_out)), map_out.shape)
    return {
        "map_db": map_out.astype(np.float32).tolist(),
        "range_axis": ranges_out.astype(np.float32).tolist(),
        "velocity_axis": velocity_out.astype(np.float32).tolist(),
        "peak_range": float(ranges_out[peak_r]),
        "peak_velocity": float(velocity_out[peak_v]),
        "peak_magnitude_db": float(map_out[peak_r, peak_v]),
        "range_resolution": float(range_resolution(chirp_bw)),
        "velocity_resolution": float(velocity_resolution(n_slow, pri, output_freq)),
        "max_velocity": float(max_unambiguous_velocity(pri, output_freq)),
        "num_chirps": int(n_slow),
    }


def ca_cfar(spectrum_db, num_guard=4, num_ref=16, bias_db=6.0):
    """Cell-averaging CFAR threshold, in dB, the same length as the input.

    For each cell under test the noise estimate is the mean power of the
    reference cells either side, skipping `num_guard` cells nearest the cell so
    a target's own skirts do not raise the threshold that is meant to detect
    it. `bias_db` is how far above that estimate a sample has to sit to count:
    raise it for fewer false alarms and more missed targets, lower it for the
    reverse. That trade is the whole point of the CFAR lab.

    Edges use however many reference cells exist rather than wrapping, since
    wrapping would let a strong near-range return set the threshold at max
    range.
    """
    spectrum_db = np.asarray(spectrum_db, dtype=float)
    if spectrum_db.ndim != 1:
        raise ValueError("ca_cfar operates on a 1D spectrum")
    n = len(spectrum_db)
    num_guard = max(0, int(num_guard))
    num_ref = int(num_ref)
    if num_ref < 1:
        raise ValueError("num_ref must be >= 1")
    if n == 0:
        return np.zeros(0)

    power = 10.0 ** (spectrum_db / 10.0)
    csum = np.concatenate([[0.0], np.cumsum(power)])

    idx = np.arange(n)
    half = num_guard + num_ref

    lo_t = np.maximum(idx - half, 0)
    hi_t = np.minimum(idx + half + 1, n)
    lo_g = np.maximum(idx - num_guard, 0)
    hi_g = np.minimum(idx + num_guard + 1, n)

    sum_ref = (csum[hi_t] - csum[lo_t]) - (csum[hi_g] - csum[lo_g])
    cnt_ref = (hi_t - lo_t) - (hi_g - lo_g)

    noise = sum_ref / np.maximum(cnt_ref, 1)
    # A cell with no reference cells at all (num_ref smaller than the guard
    # band can reach) must not read as zero noise and detect everything.
    noise = np.where(cnt_ref > 0, noise, np.max(power) if n else 1.0)

    return 10.0 * np.log10(np.maximum(noise, 1e-30)) + float(bias_db)


def apply_cfar(spectrum_db, threshold_db, floor_db=None):
    """Blank everything below the CFAR threshold.

    The lab has "Plot CFAR Threshold" and "Apply CFAR Threshold" as separate
    checkboxes on purpose: you tune the bias and cell counts against the drawn
    threshold first, and only then blank what falls below it.

    Returns (masked_spectrum, detection_indices).
    """
    spectrum_db = np.asarray(spectrum_db, dtype=float)
    threshold_db = np.asarray(threshold_db, dtype=float)
    detections = np.flatnonzero(spectrum_db > threshold_db)
    if floor_db is None:
        floor_db = float(np.min(spectrum_db)) if len(spectrum_db) else 0.0
    masked = np.where(spectrum_db > threshold_db, spectrum_db, floor_db)
    return masked, detections
