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
from phaser_functions import (
    MIN_SCAN_SNR_DB,
    find_scan_peak,
    save_hb100_cal,
    scan_peak_is_trustworthy,
    spec_est,
)

from adi import ad9361
from adi.cn0566 import CN0566

MAX_RETRIES = 3
RETRY_DELAY = 2.0

SAMPLE_RATE = 30000000

# How far to shove the LO when confirming a candidate, and how far the
# candidate's apparent absolute frequency may move before we call it a spur.
# The shift has to be large enough that a spur's absolute frequency moves well
# outside the tolerance, and small enough that the tone stays in the window.
CONFIRM_SHIFT_HZ = 4e6
CONFIRM_TOLERANCE_HZ = 1e6


def _capture(my_phaser, my_sdr, signal_freq):
    """Tune to `signal_freq` and return (amplitudes, baseband_freqs).

    The amplitudes are flipped relative to the frequency axis because the LO
    is on the high side, so the spectrum arrives inverted. That convention is
    inherited from the original script; only the ordering here defines it.
    """
    my_phaser.SignalFreq = signal_freq
    my_phaser.frequency = (int(signal_freq) + my_sdr.rx_lo) // 4
    sleep(0.05)
    my_sdr.rx()  # discard the buffer that straddles the retune
    data = my_sdr.rx()
    ampl, freqs = spec_est(data[0] + data[1], SAMPLE_RATE, ref=2 ** 12, plot=False)
    return np.flip(np.fft.fftshift(ampl)), np.fft.fftshift(freqs)


def confirm_with_lo_shift(my_phaser, my_sdr, candidate_hz):
    """Is `candidate_hz` a real tone rather than a fixed-baseband spur?

    Retune either side of the candidate and re-measure where the peak sits in
    absolute terms. A real tone keeps its absolute frequency, so it survives
    both shifts; a spur keeps its baseband offset, so its apparent absolute
    frequency moves by exactly the shift and it fails.
    """
    for shift in (+CONFIRM_SHIFT_HZ, -CONFIRM_SHIFT_HZ):
        centre = candidate_hz + shift
        ampl, bb = _capture(my_phaser, my_sdr, centre)
        observed = centre + bb[int(np.argmax(ampl))]
        error = abs(observed - candidate_hz)
        print(
            "  LO %+0.1f MHz -> peak at %.6f GHz (candidate %.6f GHz, "
            "error %.3f MHz)"
            % (shift / 1e6, observed / 1e9, candidate_hz / 1e9, error / 1e6)
        )
        if error > CONFIRM_TOLERANCE_HZ:
            return False, (
                "peak moved with the LO (%.3f MHz error at a %+0.1f MHz "
                "shift), so it is generated inside the receiver, not received"
                % (error / 1e6, shift / 1e6)
            )
    return True, "ok"


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

            # Sweep frequencies
            f_start = 10.0e9
            f_stop = 10.7e9
            f_step = 10e6

            print(f"Sweeping {f_start/1e9:.1f} GHz to {f_stop/1e9:.1f} GHz...")

            # Kept as a (steps x bins) matrix rather than flattened, because
            # spur rejection needs to compare the same baseband bin across
            # steps and flattening destroys exactly that relationship.
            rows = []
            step_freqs = []
            baseband = None
            for freq in range(int(f_start), int(f_stop), int(f_step)):
                ampl, bb = _capture(my_phaser, my_sdr, freq)
                if baseband is None:
                    baseband = bb
                rows.append(ampl)
                step_freqs.append(float(freq))
                sleep(0.05)

            result = find_scan_peak(np.array(rows), baseband, step_freqs)
            peak_hz = result["freq_hz"]

            print(f"Peak frequency found at {peak_hz / 1e9:.6f} GHz")
            print(
                "  SNR %.1f dB above residual noise, confirmed by %d LO step(s)"
                % (result["snr_db"], result["steps_confirming"])
            )
            if not result["spur_rejected"]:
                print("  NOTE: too few LO steps to build a spur template; "
                      "result is a plain peak search")
            elif abs(result["raw_freq_hz"] - peak_hz) > CONFIRM_TOLERANCE_HZ:
                # Worth saying out loud: this is the old behaviour being
                # overruled, and the gap is how far off it would have been.
                print(
                    "  spur rejection changed the answer: a plain peak search "
                    "would have returned %.6f GHz (%.1f MHz away)"
                    % (result["raw_freq_hz"] / 1e9,
                       abs(result["raw_freq_hz"] - peak_hz) / 1e6)
                )

            ok, reason = scan_peak_is_trustworthy(result, min_snr_db=MIN_SCAN_SNR_DB)
            if ok:
                print("Confirming against an LO shift...")
                ok, reason = confirm_with_lo_shift(my_phaser, my_sdr, peak_hz)

            if not ok:
                print()
                print("HB100 SEARCH FAILED: %s" % reason)
                print("Nothing was saved; the previous calibration is intact.")
                print("Check that the HB100 is powered, pointed at the array,")
                print("and roughly 1.5-2 m away, then run this again.")
                del my_sdr
                del my_phaser
                return False

            if peak_hz < 10.0e9 or peak_hz > 11.0e9:
                print(f"Warning: {peak_hz / 1e9:.6f} GHz is outside the "
                      "expected HB100 range (10.0-11.0 GHz)")

            # Save calibration
            print(f"Saving calibration: {peak_hz} Hz")
            save_hb100_cal(peak_hz)
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
