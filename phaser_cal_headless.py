#!/usr/bin/env python3
"""
Headless phaser calibration - no GUI, no prompts.
Performs channel, gain, and phase calibration automatically.

Based on the "cal" option from phaser_examples.py in pyadi-iio.
"""

import sys
import time

from phaser_functions import (
    channel_calibration,
    gain_calibration,
    load_hb100_cal,
    phase_calibration,
)

from adi import ad9361
from adi.cn0566 import CN0566

try:
    import config
except ImportError:
    print("Make sure config.py is in this directory")
    sys.exit(1)

MAX_RETRIES = 3
RETRY_DELAY = 2.0


def do_calibration():
    """Run full calibration sequence. Returns True on success."""

    my_phaser = None
    my_sdr = None

    for attempt in range(MAX_RETRIES):
        try:
            print(f"Calibration attempt {attempt + 1}/{MAX_RETRIES}...")

            # Connect to hardware (same as phaser_examples.py)
            try:
                print("Attempting to connect to CN0566 via ip:localhost...")
                my_phaser = CN0566(uri="ip:localhost")
                print("Found CN0566. Connecting to PlutoSDR via default IP address...")
                my_sdr = ad9361(uri="ip:192.168.2.1")
                print("PlutoSDR connected.")
            except Exception as e:
                print(f"Local connection failed: {e}")
                print("CN0566 on ip:localhost not found, connecting via ip:phaser.local...")
                my_phaser = CN0566(uri="ip:phaser.local")
                print("Found CN0566. Connecting to PlutoSDR via shared context...")
                my_sdr = ad9361(uri="ip:phaser.local:50901")
                print("Found SDR on shared phaser.local.")

            my_phaser.sdr = my_sdr
            time.sleep(0.5)

            # Configure device (same as phaser_examples.py)
            my_phaser.configure(device_mode="rx")
            my_phaser.SDR_init(30000000, config.Tx_freq, config.Rx_freq, 6, -6, 1024)

            # Load existing channel cal
            my_phaser.load_channel_cal()
            my_sdr.rx_hardwaregain_chan0 = my_sdr.rx_hardwaregain_chan0 + my_phaser.ccal[0]
            my_sdr.rx_hardwaregain_chan1 = my_sdr.rx_hardwaregain_chan1 + my_phaser.ccal[1]

            # Load signal frequency
            try:
                my_phaser.SignalFreq = load_hb100_cal()
                print(f"Found signal freq file: {my_phaser.SignalFreq}")
            except Exception:
                my_phaser.SignalFreq = config.SignalFreq
                print(f"No signal freq found, using config: {my_phaser.SignalFreq}")

            # Configure SDR TX (disabled for calibration with HB100)
            my_sdr.tx_hardwaregain_chan0 = int(-88)
            my_sdr.tx_hardwaregain_chan1 = int(-88)
            my_sdr.tx_lo = int(1.0e9)

            # Configure PLL
            my_phaser.frequency = (int(my_phaser.SignalFreq) + config.Rx_freq) // 4
            my_phaser.freq_dev_step = 5690
            my_phaser.freq_dev_range = 0
            my_phaser.freq_dev_time = 0
            my_phaser.powerdown = 0
            my_phaser.ramp_mode = "disabled"

            # Set initial gains
            gain_list = [127, 127, 127, 127, 127, 127, 127, 127]
            for i in range(0, len(gain_list)):
                my_phaser.set_chan_gain(i, gain_list[i], apply_cal=False)

            my_phaser.Averages = 4

            print("\n=== Starting Calibration ===")
            print("Antenna should be at mechanical boresight in front of the array")

            # Channel calibration (same as phaser_examples.py)
            print("\n--- Channel Calibration ---")
            my_phaser.set_beam_phase_diff(0.0)
            channel_calibration(my_phaser, verbose=True)
            my_phaser.save_channel_cal()
            print(f"Channel calibration saved: {my_phaser.ccal}")

            # Gain calibration (same as phaser_examples.py)
            print("\n--- Gain Calibration ---")
            my_phaser.set_beam_phase_diff(0.0)
            gain_calibration(my_phaser, verbose=True)
            my_phaser.save_gain_cal()
            print(f"Gain calibration saved: {my_phaser.gcal}")

            # Phase calibration (same as phaser_examples.py)
            print("\n--- Phase Calibration ---")
            phase_calibration(my_phaser, verbose=True)
            my_phaser.save_phase_cal()
            print(f"Phase calibration saved: {my_phaser.pcal}")

            print("\n=== Calibration Complete ===")

            # Cleanup
            if hasattr(my_sdr, "rx_destroy_buffer"):
                my_sdr.rx_destroy_buffer()
            del my_sdr
            del my_phaser

            return True

        except BrokenPipeError as e:
            print(f"Broken pipe error: {e}")
            if attempt < MAX_RETRIES - 1:
                print(f"Retrying in {RETRY_DELAY} seconds...")
                time.sleep(RETRY_DELAY)
            else:
                print("Max retries reached.")
                return False

        except Exception as e:
            print(f"Error: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            if attempt < MAX_RETRIES - 1:
                print(f"Retrying in {RETRY_DELAY} seconds...")
                time.sleep(RETRY_DELAY)
            else:
                print("Max retries reached.")
                return False

        finally:
            # Cleanup on error
            try:
                if my_sdr is not None:
                    if hasattr(my_sdr, "rx_destroy_buffer"):
                        my_sdr.rx_destroy_buffer()
                    del my_sdr
            except Exception:
                pass
            try:
                if my_phaser is not None:
                    del my_phaser
            except Exception:
                pass

    return False


if __name__ == "__main__":
    success = do_calibration()
    sys.exit(0 if success else 1)
