#!/usr/bin/env python3
"""
Headless HB100 frequency finder - no GUI, no prompts.
Automatically saves calibration file on success.

Based on phaser_find_hb100.py from Analog Devices.
"""

import os
import pickle
import socket
import sys
import time
from time import sleep

import numpy as np
from phaser_functions import save_hb100_cal, spec_est

from adi import ad9361
from adi.cn0566 import CN0566

MAX_RETRIES = 3
RETRY_DELAY = 2.0


def find_hb100():
    """Find HB100 frequency and save calibration. Returns True on success."""

    my_phaser = None
    my_sdr = None

    for attempt in range(MAX_RETRIES):
        try:
            print(f"Attempt {attempt + 1}/{MAX_RETRIES}...")

            # Connect to CN0566 and SDR
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

            # Configure device
            my_phaser.configure(device_mode="rx")

            # Configure SDR parameters
            my_sdr._ctrl.debug_attrs["adi,frequency-division-duplex-mode-enable"].value = "1"
            my_sdr._ctrl.debug_attrs["adi,ensm-enable-txnrx-control-enable"].value = "0"
            my_sdr._ctrl.debug_attrs["initialize"].value = "1"

            my_sdr.rx_enabled_channels = [0, 1]
            my_sdr._rxadc.set_kernel_buffers_count(1)
            rx = my_sdr._ctrl.find_channel("voltage0")
            rx.attrs["quadrature_tracking_en"].value = "1"
            my_sdr.sample_rate = int(30000000)
            my_sdr.rx_buffer_size = int(4 * 256)
            my_sdr.rx_rf_bandwidth = int(10e6)
            my_sdr.gain_control_mode_chan0 = "manual"
            my_sdr.gain_control_mode_chan1 = "manual"
            my_sdr.rx_hardwaregain_chan0 = 0
            my_sdr.rx_hardwaregain_chan1 = 0
            my_sdr.rx_lo = int(2.0e9)

            # Load filter if available
            try:
                my_sdr.filter = "LTE20_MHz.ftr"
            except Exception as e:
                print(f"Warning: Could not load filter: {e}")

            my_sdr.tx_hardwaregain_chan0 = int(-80)
            my_sdr.tx_hardwaregain_chan1 = int(-80)

            # Configure phaser
            my_phaser.SignalFreq = 10.525e9
            my_phaser.lo = int(my_phaser.SignalFreq) + my_sdr.rx_lo

            gain_list = [64] * 8
            for i in range(0, len(gain_list)):
                my_phaser.set_chan_gain(i, gain_list[i], apply_cal=False)

            my_phaser.set_beam_phase_diff(0.0)
            my_phaser.Averages = 8

            # Initialize arrays
            full_ampl = np.empty(0)
            full_freqs = np.empty(0)

            # Sweep frequencies
            f_start = 10.0e9
            f_stop = 10.7e9
            f_step = 10e6

            print(f"Sweeping {f_start/1e9:.1f} GHz to {f_stop/1e9:.1f} GHz...")

            for freq in range(int(f_start), int(f_stop), int(f_step)):
                my_phaser.SignalFreq = freq
                my_phaser.frequency = (int(my_phaser.SignalFreq) + my_sdr.rx_lo) // 4

                data = my_sdr.rx()
                data_sum = data[0] + data[1]
                ampl, freqs = spec_est(data_sum, 30000000, ref=2 ^ 12, plot=False)
                ampl = np.fft.fftshift(ampl)
                ampl = np.flip(ampl)
                freqs = np.fft.fftshift(freqs)
                freqs += freq
                full_freqs = np.concatenate((full_freqs, freqs))
                full_ampl = np.concatenate((full_ampl, ampl))
                sleep(0.1)

            full_freqs /= 1e9  # Hz -> GHz

            # Find peak
            peak_index = np.argmax(full_ampl)
            peak_freq = full_freqs[peak_index]
            print(f"Peak frequency found at {peak_freq:.6f} GHz")

            # Validate - peak should be in expected HB100 range
            if peak_freq < 10.0 or peak_freq > 11.0:
                print(f"Warning: Peak frequency {peak_freq} GHz outside expected range (10.0-11.0 GHz)")
                print("This may not be the HB100 signal.")

            # Check signal strength
            peak_ampl = full_ampl[peak_index]
            noise_floor = np.median(full_ampl)
            snr = peak_ampl - noise_floor
            print(f"Peak amplitude: {peak_ampl:.1f} dB, Noise floor: {noise_floor:.1f} dB, SNR: {snr:.1f} dB")

            if snr < 10:
                print("Warning: Low SNR - HB100 signal may be weak or not present")

            # Save calibration
            print(f"Saving calibration: {peak_freq * 1e9} Hz")
            save_hb100_cal(peak_freq * 1e9)
            print("Calibration saved successfully!")

            # Cleanup
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
            print(f"Error: {e}")
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
                    del my_sdr
            except:
                pass
            try:
                if my_phaser is not None:
                    del my_phaser
            except:
                pass

    return False


if __name__ == "__main__":
    success = find_hb100()
    sys.exit(0 if success else 1)
