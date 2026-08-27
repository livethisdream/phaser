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
