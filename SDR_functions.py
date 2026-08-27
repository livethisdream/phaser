import adi
import os
import time
import numpy as np

from phaser_functions import load_cal_values


def load_channel_cal(default=None, filename=None):
    """Two-channel SDR gain correction, from calibration.json.

    Falls back to the legacy channel_cal_val.pkl. `filename` is accepted and
    ignored for backwards compatibility.
    """
    if default is None:
        default = [0.0] * 2
    return load_cal_values("channel_cal", default, 2)


def _set_channel_map_with_fallback(sdr, attr_name, preferred):
    try:
        setattr(sdr, attr_name, preferred)
        return preferred
    except Exception as e:
        fallback = [0]
        setattr(sdr, attr_name, fallback)
        print(f"{attr_name} fallback to {fallback}: {e}")
        return fallback

def SDR_init(ip, sample_rate, tx_lo, rx_lo, rx_gain, tx_gain, buffer_size=1024*16):
    print(f"Connecting to SDR at {ip}...")
    # Use ad9361 instead of Pluto for better dual-channel support through iiod proxy
    sdr = adi.ad9361(uri=ip)

    # Prefer dual-channel mode, but gracefully fallback for 1-channel device mappings.
    rx_map = _set_channel_map_with_fallback(sdr, "rx_enabled_channels", [0, 1])
    tx_map = _set_channel_map_with_fallback(sdr, "tx_enabled_channels", [0, 1])
    print(f"SDR channel map: rx={rx_map}, tx={tx_map}")

    sdr.sample_rate = int(sample_rate)
    
    # Configure Rx
    sdr.rx_lo = int(rx_lo)
    sdr.rx_rf_bandwidth = int(sample_rate)
    sdr.rx_buffer_size = int(buffer_size)
    sdr.gain_control_mode_chan0 = "manual"
    sdr.rx_hardwaregain_chan0 = int(rx_gain)
    if len(rx_map) > 1:
        sdr.gain_control_mode_chan1 = "manual"
        sdr.rx_hardwaregain_chan1 = int(rx_gain)
    
    # Configure Tx
    sdr.tx_lo = int(tx_lo)
    sdr.tx_rf_bandwidth = int(sample_rate)
    sdr.tx_cyclic_buffer = True
    sdr.tx_hardwaregain_chan0 = int(tx_gain)
    if len(tx_map) > 1:
        sdr.tx_hardwaregain_chan1 = int(tx_gain)

    # Need a small delay before receiving
    time.sleep(0.5)

    return sdr

def SDR_LO_init(rpi_ip, lo_freq):
    """Initialise the ADF4159 LO and return the synth object so callers can
    keep it alive (preventing GC from closing the libiio context)."""
    print(f"Initializing external LO on {rpi_ip} to {lo_freq} Hz")
    try:
        synth = adi.adf4159(rpi_ip)
        synth.frequency = int(lo_freq)
        return synth
    except Exception as e:
        print(f"Failed to set ADF4159 LO: {e}")
        return None

def SDR_setRx(sdr, rx0_gain, rx1_gain):
    sdr.rx_hardwaregain_chan0 = int(rx0_gain)
    if hasattr(sdr, "rx_hardwaregain_chan1"):
        try:
            sdr.rx_hardwaregain_chan1 = int(rx1_gain)
        except Exception:
            pass

def SDR_setTx(sdr, tx_gain):
    sdr.tx_hardwaregain_chan0 = int(tx_gain)
    if hasattr(sdr, "tx_hardwaregain_chan1"):
        try:
            sdr.tx_hardwaregain_chan1 = int(tx_gain)
        except Exception:
            pass

def SDR_getData(sdr):
    # Always return [chan0_data, chan1_data] as 1D arrays.
    # Some pyadi/iio paths may return a single ndarray when one RX channel is active.
    raw = sdr.rx()

    if isinstance(raw, np.ndarray):
        if raw.ndim == 1:
            return [raw, raw]
        if raw.ndim >= 2 and raw.shape[0] >= 2:
            return [raw[0], raw[1]]
        flattened = np.ravel(raw)
        return [flattened, flattened]

    if isinstance(raw, (list, tuple)):
        if len(raw) >= 2:
            return [np.asarray(raw[0]), np.asarray(raw[1])]
        if len(raw) == 1:
            ch = np.asarray(raw[0])
            return [ch, ch]

    ch = np.asarray(raw)
    return [ch, ch]

def SDR_TxBuffer_Destroy(sdr):
    """Legacy function kept for compatibility but no longer needed."""
    # The TX cyclic buffer cleans up automatically; don't call
    # sdr.tx_destroy_buffer() as it can cause access violations on cleanup
    pass

