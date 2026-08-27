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
    """Set the CN0566 LO to `lo_freq` Hz and return the synth object.

    The object is returned so callers can keep it alive; letting it be garbage
    collected closes the libiio context underneath it.

    `lo_freq` is the LO the mixer sees. The ADF4159 register takes a QUARTER of
    that, because the CN0566 divides by 4 ahead of the PLL's RFIN -- pyadi-iio's
    own CN0566.lo setter is literally `self.frequency = int(value / 4)`, and
    every ADI phaser example writes `(SignalFreq + Rx_freq) // 4`.

    This helper wrote `lo_freq` undivided, which asked for an LO four times too
    high. Nothing complained: the attribute write is accepted and reads straight
    back, so there was no error to notice -- the PLL simply could not lock there
    and the array received noise. Both calibration scripts do their own division
    and so always worked, which is exactly why "find HB100 sees 53 dB SNR but
    the live sweep shows only the noise floor".
    """
    target = int(lo_freq / 4)
    print(f"Initializing external LO on {rpi_ip} to {lo_freq} Hz "
          f"(ADF4159 register {target} Hz, /4)")
    try:
        synth = adi.adf4159(rpi_ip)
        synth.frequency = target
        # Read back. The write is accepted silently even when the value is
        # nonsense, so a mismatch here is the only way to catch a bad LO before
        # it shows up as an inexplicably empty spectrum.
        actual = int(synth.frequency)
        if abs(actual - target) > 1000:
            print(f"WARNING: ADF4159 readback {actual} Hz != requested {target} Hz "
                  f"-- LO is not where it should be; expect no signal.")
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

