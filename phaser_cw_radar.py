"""
CW (continuous-wave) Doppler radar helpers for the Phaser headless backend.

Mode is enter/exit only — the caller (phaser_headless.py) drives the per-frame
capture in its main loop. All functions here are pure / SDR-poking helpers; this
module never touches the WebSocket, ZMQ, or the headless object's state
directly.

Hardware contract:
  - The SDR object is an `adi.ad9361` already initialized by SDR_init().
  - The ADAR1000 array is already configured for Rx beamforming.
  - On `enter_cw_mode`, the SDR is reconfigured for the requested CW capture
    parameters (sample rate, FFT/buffer size, IF tone) and a cyclic Tx tone is
    started. The previous SDR config is captured into `saved_state` so
    `exit_cw_mode` can restore it.

Defaults follow radar/CW_RADAR_Waterfall.py:
  sample_rate = 600 kHz, fft_size = 64K, signal_freq = 100 kHz IF tone,
  rx_gain = 30 dB, tx_gain ch1 = 0 dB, tx_gain ch0 = -88 dB (off),
  taper = Blackman [8, 34, 84, 127, 127, 84, 34, 8].
"""

import time
import numpy as np

from SDR_functions import SDR_getData


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
}

TAPER_PRESETS = {
    "rect":     [127, 127, 127, 127, 127, 127, 127, 127],
    "hann":     [12,  43,  77,  100, 100, 77,  43,  12],
    # Match radar/CW_RADAR_Waterfall.py exactly:
    "blackman": [8,   34,  84,  127, 127, 84,  34,  8],
}


def _build_iq_tone(fs, n, signal_freq, scale=2**14):
    """Build a cyclic IQ tone at the requested IF frequency, snapped to bin."""
    fc_bin = round(signal_freq / fs * n)
    fc_exact = fc_bin * fs / n
    ts = 1.0 / fs
    t = np.arange(0, n * ts, ts)
    i = np.cos(2 * np.pi * t * fc_exact) * scale
    q = np.sin(2 * np.pi * t * fc_exact) * scale
    return i + 1j * q


def enter_cw_mode(sdr, params, saved_state=None):
    """Reconfigure the SDR for CW radar capture.

    Args:
        sdr: pyadi adi.ad9361 instance
        params: dict (any DEFAULTS keys); missing keys filled from DEFAULTS
        saved_state: dict to populate with prior SDR settings for restoration

    Returns:
        Effective params dict (with defaults applied) — caller stores this.
    """
    cfg = {**DEFAULTS, **(params or {})}
    fs = int(cfg["sample_rate"])
    n  = int(cfg["fft_size"])
    sig_freq = float(cfg["signal_freq"])
    rx_lo = int(cfg["center_freq"])
    tx_lo = int(cfg["center_freq"])
    rx_gain = int(cfg["rx_gain"])
    tx_gain = int(cfg["tx_gain"])

    if saved_state is not None:
        try:
            saved_state["sample_rate"] = int(sdr.sample_rate)
            saved_state["rx_rf_bandwidth"] = int(sdr.rx_rf_bandwidth)
            saved_state["rx_buffer_size"] = int(sdr.rx_buffer_size)
            saved_state["rx_lo"] = int(sdr.rx_lo)
            saved_state["tx_lo"] = int(sdr.tx_lo)
            saved_state["rx_hardwaregain_chan0"] = int(sdr.rx_hardwaregain_chan0)
            saved_state["tx_hardwaregain_chan0"] = int(sdr.tx_hardwaregain_chan0)
            if hasattr(sdr, "rx_hardwaregain_chan1"):
                saved_state["rx_hardwaregain_chan1"] = int(sdr.rx_hardwaregain_chan1)
            if hasattr(sdr, "tx_hardwaregain_chan1"):
                saved_state["tx_hardwaregain_chan1"] = int(sdr.tx_hardwaregain_chan1)
        except Exception as e:
            print(f"[CW] Warning: could not snapshot SDR state: {e}")

    # Configure SDR for CW
    sdr.sample_rate = fs
    sdr.rx_rf_bandwidth = fs
    sdr.rx_buffer_size = n
    sdr.rx_lo = rx_lo
    sdr.tx_lo = tx_lo
    sdr.gain_control_mode_chan0 = "manual"
    sdr.rx_hardwaregain_chan0 = rx_gain
    if hasattr(sdr, "rx_hardwaregain_chan1"):
        try:
            sdr.gain_control_mode_chan1 = "manual"
            sdr.rx_hardwaregain_chan1 = rx_gain
        except Exception:
            pass

    # Tx: ch0 off, ch1 full (matches CW_RADAR_Waterfall.py — tone goes out OUT2)
    try:
        sdr.tx_hardwaregain_chan0 = -88
    except Exception:
        pass
    if hasattr(sdr, "tx_hardwaregain_chan1"):
        try:
            sdr.tx_hardwaregain_chan1 = tx_gain
        except Exception:
            pass

    # Build cyclic IQ tone
    iq = _build_iq_tone(fs, n, sig_freq)
    try:
        sdr._ctx.set_timeout(0)
    except Exception:
        pass

    # Stop any prior cyclic Tx before loading a new buffer
    try:
        sdr.tx_destroy_buffer()
    except Exception:
        pass

    sdr.tx([iq * 0.5, iq])  # ch0 reduced (it's gain-disabled anyway), ch1 full

    time.sleep(0.2)
    return cfg


def exit_cw_mode(sdr, saved_state):
    """Restore SDR to its pre-CW configuration. Tolerant of a missing snapshot."""
    try:
        sdr.tx_destroy_buffer()
    except Exception:
        pass

    if not saved_state:
        return

    try:
        if "sample_rate" in saved_state:
            sdr.sample_rate = int(saved_state["sample_rate"])
        if "rx_rf_bandwidth" in saved_state:
            sdr.rx_rf_bandwidth = int(saved_state["rx_rf_bandwidth"])
        if "rx_buffer_size" in saved_state:
            sdr.rx_buffer_size = int(saved_state["rx_buffer_size"])
        if "rx_lo" in saved_state:
            sdr.rx_lo = int(saved_state["rx_lo"])
        if "tx_lo" in saved_state:
            sdr.tx_lo = int(saved_state["tx_lo"])
        if "rx_hardwaregain_chan0" in saved_state:
            sdr.rx_hardwaregain_chan0 = int(saved_state["rx_hardwaregain_chan0"])
        if "rx_hardwaregain_chan1" in saved_state and hasattr(sdr, "rx_hardwaregain_chan1"):
            try:
                sdr.rx_hardwaregain_chan1 = int(saved_state["rx_hardwaregain_chan1"])
            except Exception:
                pass
        if "tx_hardwaregain_chan0" in saved_state:
            sdr.tx_hardwaregain_chan0 = int(saved_state["tx_hardwaregain_chan0"])
        if "tx_hardwaregain_chan1" in saved_state and hasattr(sdr, "tx_hardwaregain_chan1"):
            try:
                sdr.tx_hardwaregain_chan1 = int(saved_state["tx_hardwaregain_chan1"])
            except Exception:
                pass
    except Exception as e:
        print(f"[CW] Warning: SDR restore failed: {e}")


def _window(name, n):
    name = (name or "").lower()
    if name in ("hann", "hanning"):
        return np.hanning(n)
    if name == "hamming":
        return np.hamming(n)
    if name == "rect" or name == "none":
        return np.ones(n)
    return np.blackman(n)  # default


def capture_cw_frame(sdr):
    """Pull one Rx buffer; return summed IQ (channel 0 + channel 1)."""
    data = SDR_getData(sdr)
    return data[0] + data[1]


def process_cw_frame(iq, fs, signal_freq, output_freq, fft_window="blackman",
                     downsample_iq=512):
    """Compute Doppler spectrum, velocity axis, and stats for one CW frame.

    The CW radar transmits at signal_freq above DC (the IF tone). A moving
    target reflects with a Doppler shift around that tone; we shift the spectrum
    so the IF appears at 0 Hz, then convert that residual frequency to velocity:

        v = c * f_doppler / (2 * f_carrier)

    where f_carrier is the actual transmitted carrier (== output_freq for the
    CN0566 CW path).
    """
    n = int(len(iq))
    if n == 0:
        raise ValueError("Empty IQ buffer")

    win = _window(fft_window, n)
    sp = np.abs(np.fft.fft(iq * win))
    sp = np.fft.fftshift(sp)
    mag = np.maximum(sp / np.sum(win), 1e-15)
    spectrum_db = 20 * np.log10(mag / (2 ** 11))

    freq = np.fft.fftshift(np.fft.fftfreq(n, 1.0 / fs))

    # Doppler frequency = freq - signal_freq (IF tone moves to 0 after shift)
    doppler_hz = freq - signal_freq

    c = 299_792_458.0
    f_carrier = float(output_freq) if output_freq else 1.0
    velocity = c * doppler_hz / (2.0 * f_carrier)

    # Crop to a reasonable Doppler window so the frontend isn't sent 64k bins.
    # Default: ±20 m/s (covers normal indoor speeds well). Frontend can request
    # a wider window in v2.
    vel_max = 30.0  # m/s (slightly larger than UI default to give headroom)
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
