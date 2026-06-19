#!/usr/bin/env python3
"""
Headless phaser calibration - no GUI, no prompts.
Performs channel, gain, and phase calibration automatically.

Based on phaser_cal.py from Analog Devices.
"""

import os
import socket
import sys
import time

import pickle
import adi
from phaser_functions import (
    channel_calibration,
    gain_calibration,
    load_hb100_cal,
    phase_calibration,
)


def save_channel_cal(values, filename="channel_cal_val.pkl"):
    """Save channel calibration values to pickle file."""
    with open(filename, "wb") as f:
        pickle.dump(values, f)


def save_gain_cal(values, filename="gain_cal_val.pkl"):
    """Save gain calibration values to pickle file."""
    with open(filename, "wb") as f:
        pickle.dump(values, f)


def save_phase_cal(values, filename="phase_cal_val.pkl"):
    """Save phase calibration values to pickle file."""
    with open(filename, "wb") as f:
        pickle.dump(values, f)

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

            # Detect hostname and set URIs
            if socket.gethostname().find(".") >= 0:
                hostname = socket.gethostname()
            else:
                hostname = socket.gethostbyaddr(socket.gethostname())[0]

            if "phaser" in hostname:
                rpi_ip = "ip:localhost"
                sdr_ip = "ip:192.168.2.1"
            else:
                rpi_ip = "ip:phaser.local"
                sdr_ip = "ip:phaser.local:50901"

            print(f"Hostname: {hostname}, rpi: {rpi_ip}, sdr: {sdr_ip}")

            # Connect to hardware
            print("Connecting to SDR...")
            my_sdr = adi.ad9361(uri=sdr_ip)
            print("Connecting to CN0566...")
            my_phaser = adi.CN0566(uri=rpi_ip, sdr=my_sdr)
            my_phaser.sdr = my_sdr
            time.sleep(0.5)

            # Configure device
            print("Configuring device...")
            my_phaser.configure(device_mode="rx")
            my_phaser.SDR_init(30000000, config.Tx_freq, config.Rx_freq, 6, -6, 1024)

            # Load existing channel cal
            my_phaser.load_channel_cal()
            my_sdr.rx_hardwaregain_chan0 = my_sdr.rx_hardwaregain_chan0 + my_phaser.ccal[0]
            my_sdr.rx_hardwaregain_chan1 = my_sdr.rx_hardwaregain_chan1 + my_phaser.ccal[1]

            # Load signal frequency
            try:
                my_phaser.SignalFreq = load_hb100_cal()
                print(f"Loaded HB100 freq: {my_phaser.SignalFreq}")
            except Exception:
                my_phaser.SignalFreq = config.SignalFreq
                print(f"Using config SignalFreq: {my_phaser.SignalFreq}")

            # Disable TX path
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
            my_phaser.Averages = 4

            # Aim at boresight
            my_phaser.set_beam_phase_diff(0.0)

            print("\n=== Starting Calibration ===")
            print("Ensure antenna is at mechanical boresight in front of the array")

            # Channel calibration
            print("\n--- Channel Calibration ---")
            my_phaser.set_beam_phase_diff(0.0)
            channel_calibration(my_phaser, verbose=True)
            ccal = getattr(my_phaser, "ccal", [0.0, 0.0])
            save_channel_cal(ccal)
            print(f"Channel calibration saved: {ccal}")

            # Gain calibration
            print("\n--- Gain Calibration ---")
            my_phaser.set_beam_phase_diff(0.0)
            gain_calibration(my_phaser, verbose=True)
            gcal = getattr(my_phaser, "gcal", [1.0] * 8)
            save_gain_cal(gcal)
            print(f"Gain calibration saved: {gcal}")

            # Phase calibration
            print("\n--- Phase Calibration ---")
            phase_calibration(my_phaser, verbose=True)
            pcal = getattr(my_phaser, "pcal", [0.0] * 8)
            save_phase_cal(pcal)
            print(f"Phase calibration saved: {pcal}")

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
