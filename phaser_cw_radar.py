"""CW (continuous-wave) Doppler radar helpers for the Phaser headless backend.

Mode is enter/exit only — the caller (phaser_headless.py) drives the per-frame
capture in its main loop. All functions here are SDR/array-poking helpers; this
module never touches the WebSocket, ZMQ, or the headless object's state
directly. The signal processing and the configuration tables live in
phaser_radar_dsp.py, which imports no hardware and so stays testable; the names
are re-exported here for callers that already import them from this module.

Hardware contract:
  - The SDR object is an `adi.ad9361` already initialized by SDR_init().
  - The ADAR1000 array is already configured for Rx beamforming.
  - On `enter_cw_mode`, the SDR is reconfigured for the requested CW capture
    parameters (sample rate, FFT/buffer size, IF tone), the Phaser's ADF4159 LO
    is moved to the CW mixing frequency, the requested taper is latched into
    the array, and a cyclic Tx tone is started. The previous configuration is
    captured into `saved_state` so `exit_cw_mode` can restore it.

Defaults follow radar/CW_RADAR_Waterfall.py:
  sample_rate = 600 kHz, fft_size = 64K, signal_freq = 100 kHz IF tone,
  rx_gain = 30 dB, tx_gain ch1 = 0 dB, tx_gain ch0 = -88 dB (off),
  taper = Blackman [8, 34, 84, 127, 127, 84, 34, 8].
"""

import time

from ADAR_pyadi_functions import ADAR_set_Taper
from SDR_functions import SDR_getData, SDR_LO_init

# Re-exported so `from phaser_cw_radar import process_cw_frame, DEFAULTS` and
# the other historical import sites keep resolving.
from phaser_radar_dsp import (  # noqa: F401
    C,
    DEFAULTS,
    TAPER_PRESETS,
    build_iq_tone,
    cw_lo_freq,
    process_cw_frame,
    resolve_taper,
    window,
    _window,
)

# Historical private name for the tone builder.
_build_iq_tone = build_iq_tone


def apply_taper(array, taper):
    """Latch a taper into the ADAR1000 array. Returns the gains actually sent.

    Returns None when there is no array to write to (sim mode passes one, but
    a caller that has not got that far can pass None).
    """
    gains = resolve_taper(taper)
    if array is None:
        return None
    try:
        ADAR_set_Taper(array, gains)
    except Exception as e:
        print(f"[CW] Warning: could not apply taper {gains}: {e}")
        return None
    return gains


def _tx_channel_count(sdr):
    """How many Tx channels this SDR actually has enabled.

    SDR_init falls back to a single-channel map when the device mapping does
    not support two, so the Tx payload has to match or pyadi raises.
    """
    try:
        enabled = sdr.tx_enabled_channels
    except Exception:
        return 2
    try:
        return max(1, len(enabled))
    except TypeError:
        return 1


def enter_cw_mode(sdr, params, saved_state=None, array=None, rpi_ip=None):
    """Reconfigure the SDR, LO and array for CW radar capture.

    Args:
        sdr: pyadi adi.ad9361 instance
        params: dict (any DEFAULTS keys); missing keys filled from DEFAULTS
        saved_state: dict to populate with prior settings for restoration
        array: ADAR1000 array, so the CW taper can be latched. Optional.
        rpi_ip: Phaser's IP, so the ADF4159 can be moved to the CW mixing
            frequency. Optional — pass None (as sim mode does) to leave the LO
            alone.

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

    # Move the Phaser's ADF4159 to the CW mixing frequency.
    #
    # This is not optional housekeeping. Without it the LO stays wherever the
    # beamforming path left it -- at (SignalFreq + Rx_freq), the frequency that
    # receives the HB100 -- so the CW tone was transmitted nowhere near
    # `output_freq`, while process_cw_frame went on dividing the Doppler shift
    # by `output_freq` to get velocity. The mode ran, drew a spectrum, and
    # reported velocities against a carrier the hardware was not using.
    if rpi_ip:
        lo = cw_lo_freq(cfg)
        if saved_state is not None:
            saved_state["restore_lo"] = True
        try:
            SDR_LO_init(rpi_ip, lo)
            print(f"[CW] LO set to {lo/1e9:.6f} GHz "
                  f"(output {float(cfg['output_freq'])/1e9:.3f} GHz)")
        except Exception as e:
            print(f"[CW] Warning: could not set LO: {e}")

    # Latch the CW taper into the array. The taper was previously carried in
    # DEFAULTS and pushed by the frontend, but nothing ever wrote it to the
    # ADAR1000 -- the array kept whatever the beamforming sweep last latched.
    gains = apply_taper(array, cfg.get("taper"))
    if gains is not None:
        print(f"[CW] Taper applied: {gains}")

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
    iq = build_iq_tone(fs, n, sig_freq)
    try:
        sdr._ctx.set_timeout(0)
    except Exception:
        pass

    # Stop any prior cyclic Tx before loading a new buffer
    try:
        sdr.tx_destroy_buffer()
    except Exception:
        pass

    # Match the payload to the enabled Tx map: a single-channel fallback
    # rejects a two-element list.
    if _tx_channel_count(sdr) > 1:
        sdr.tx([iq * 0.5, iq])  # ch0 reduced (it's gain-disabled anyway), ch1 full
    else:
        sdr.tx(iq)

    time.sleep(0.2)
    return cfg


def exit_cw_mode(sdr, saved_state, array=None, rpi_ip=None, restore_taper=None,
                 restore_lo_freq=None):
    """Restore the SDR, LO and array to their pre-CW configuration.

    Tolerant of a missing snapshot: a half-entered mode still has to be
    leavable, or a failed enter strands the hardware in CW config.
    """
    try:
        sdr.tx_destroy_buffer()
    except Exception:
        pass

    # Put the LO back where the beamforming path expects it, otherwise the
    # next sweep runs against the CW mixing frequency and sees nothing.
    if rpi_ip and restore_lo_freq and (saved_state or {}).get("restore_lo"):
        try:
            SDR_LO_init(rpi_ip, restore_lo_freq)
            print(f"[CW] LO restored to {float(restore_lo_freq)/1e9:.6f} GHz")
        except Exception as e:
            print(f"[CW] Warning: could not restore LO: {e}")

    # Put the beamforming taper back, for the same reason.
    if array is not None and restore_taper is not None:
        apply_taper(array, restore_taper)

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


def capture_cw_frame(sdr):
    """Pull one Rx buffer; return summed IQ (channel 0 + channel 1)."""
    data = SDR_getData(sdr)
    return data[0] + data[1]
