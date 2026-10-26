#!/usr/bin/env python3
"""Is the LO cliff on this kit an analog limit, or a PLL that failed to relock?

phaser-00 reads 86 dB SNR at Rx_freq 2.175 GHz and 13 dB at 2.200 -- 75 dB
across one 25 MHz step, which is far too sharp for a rolloff and looks much
more like the ADF4159 dropping lock. If it is a lock failure, the VCO band is
chosen by an autocal the driver runs on a frequency write, and the answer is a
code fix that restores the whole range. If it is analog, the only answer is an
IF that stays below the edge.

Three questions, in order:

  1. where exactly is the edge (5 MHz steps);
  2. does it depend on the direction of approach -- hysteresis would mean the
     VCO is staying in a band chosen lower down, i.e. no autocal;
  3. does any relock action recover the tone at 2.200 once it is lost.

Read-only: saves nothing, and puts the radio back at the end.
"""

import sys
import time

import numpy as np

from phaser_functions import load_hb100_cal, lo_chain, spec_est

from adi import ad9361
from adi.cn0566 import CN0566

import config

MAX_GAIN = 127


def connect_and_setup(signal_freq, attempts=3):
    for attempt in range(attempts):
        try:
            phaser = CN0566(uri="ip:localhost")
            sdr = ad9361(uri="ip:192.168.2.1")
            phaser.sdr = sdr
            time.sleep(0.5)
            phaser.configure(device_mode="rx")
            phaser.SDR_init(30000000, config.Tx_freq, config.Rx_freq, 6, -6, 1024)
            phaser.SignalFreq = signal_freq
            sdr.tx_hardwaregain_chan0 = int(-88)
            sdr.tx_hardwaregain_chan1 = int(-88)
            sdr.tx_lo = int(1.0e9)
            phaser.freq_dev_step = 5690
            phaser.freq_dev_range = 0
            phaser.freq_dev_time = 0
            phaser.powerdown = 0
            phaser.ramp_mode = "disabled"
            for chan in range(8):
                phaser.set_chan_gain(chan, MAX_GAIN, apply_cal=False)
            phaser.Averages = 4
            return phaser, sdr
        except BrokenPipeError as e:
            print(f"  broken pipe {attempt + 1}/{attempts}: {e}")
            if attempt == attempts - 1:
                raise
            time.sleep(2.0)


def measure(sdr, averages=2):
    """(peak dBFS, SNR over the median bin) with a freshly flushed buffer."""
    peaks = []
    spec = None
    for _ in range(averages):
        try:
            sdr.rx_destroy_buffer()
        except Exception:
            pass
        raw = sdr.rx()
        ampl, _ = spec_est(np.asarray(raw[0]) + np.asarray(raw[1]),
                           int(sdr.sample_rate), ref=2 ** 11, plot=False)
        spec = np.fft.fftshift(ampl)
        peaks.append(float(np.max(spec)))
    peak = float(np.mean(peaks))
    return peak, peak - float(np.median(spec))


def tune(phaser, sdr, signal_freq, rx_freq):
    chain = lo_chain(signal_freq, rx_freq)
    try:
        sdr.rx_destroy_buffer()
    except Exception:
        pass
    sdr.rx_lo = int(rx_freq)
    phaser.frequency = int(chain["adf4159_hz"])
    time.sleep(0.2)
    return chain


def sweep(phaser, sdr, signal_freq, freqs, title):
    print("\n=== %s ===" % title)
    print("  %8s %10s %12s %9s %8s %14s" %
          ("Rx_freq", "LO (GHz)", "ADF4159 req", "peak", "SNR", "ADF4159 read"))
    rows = []
    for rx_freq in freqs:
        chain = tune(phaser, sdr, signal_freq, rx_freq)
        peak, snr = measure(sdr)
        try:
            readback = float(phaser.frequency) / 1e9
        except Exception:
            readback = float("nan")
        rows.append((rx_freq, peak, snr))
        print("  %8.4f %10.4f %12.5f %9.2f %8.1f %14.5f" %
              (rx_freq / 1e9, chain["lo_hz"] / 1e9, chain["adf4159_hz"] / 1e9,
               peak, snr, readback))
    return rows


def relock_attempts(phaser, sdr, signal_freq):
    """Lose the tone at 2.200, then try to get it back without leaving 2.200.

    Each action is followed by a measurement at the SAME IF. Anything that
    recovers the tone proves the hardware can reach this LO and the driver
    simply was not told to recalibrate -- which is a bug worth fixing rather
    than a limit worth avoiding.
    """
    dead = 2.200e9
    print("\n=== Relock attempts at Rx_freq 2.200 GHz ===")
    chain = lo_chain(signal_freq, dead)
    adf = int(chain["adf4159_hz"])

    def shot(label):
        peak, snr = measure(sdr)
        print("  %-44s %9.2f dBFS %8.1f dB" % (label, peak, snr))
        return snr

    tune(phaser, sdr, signal_freq, dead)
    shot("baseline (arrived from wherever we were)")

    phaser.frequency = adf
    time.sleep(0.3)
    shot("rewrite the same frequency")

    phaser.ramp_mode = "disabled"
    phaser.frequency = adf
    time.sleep(0.3)
    shot("rewrite ramp_mode=disabled, then frequency")

    try:
        phaser.powerdown = 1
        time.sleep(0.2)
        phaser.powerdown = 0
        phaser.frequency = adf
        time.sleep(0.3)
        shot("powerdown 1 -> 0, then frequency")
    except Exception as e:
        print("  powerdown toggle failed: %s" % e)

    # Walk up in small steps: if the part can autocal its way there, a
    # staircase reaches what a jump cannot.
    for step in (2.180e9, 2.190e9, 2.195e9, 2.200e9):
        tune(phaser, sdr, signal_freq, step)
    shot("staircase 2.180 -> 2.190 -> 2.195 -> 2.200")

    # Approach from above, in case the band chosen higher up covers it.
    tune(phaser, sdr, signal_freq, 2.400e9)
    tune(phaser, sdr, signal_freq, dead)
    shot("approach from 2.400 GHz above")

    try:
        phaser.configure(device_mode="rx")
        phaser.frequency = adf
        time.sleep(0.5)
        for chan in range(8):
            phaser.set_chan_gain(chan, MAX_GAIN, apply_cal=False)
        shot("full reconfigure(device_mode=rx), then frequency")
    except Exception as e:
        print("  reconfigure failed: %s" % e)


def main():
    try:
        signal_freq = load_hb100_cal()
    except Exception:
        signal_freq = config.SignalFreq
    print("HB100: %.6f GHz" % (signal_freq / 1e9))

    phaser, sdr = connect_and_setup(signal_freq)

    up = list(np.arange(2.150e9, 2.2255e9, 5e6))
    sweep(phaser, sdr, signal_freq, up, "Fine sweep UP (5 MHz steps)")
    sweep(phaser, sdr, signal_freq, list(reversed(up)),
          "Fine sweep DOWN (hysteresis check)")

    relock_attempts(phaser, sdr, signal_freq)

    tune(phaser, sdr, signal_freq, 2.0e9)
    peak, snr = measure(sdr)
    print("\nSanity: back at 2.000 GHz -> %.2f dBFS, %.1f dB SNR" % (peak, snr))
    print("Done. Nothing was saved.")


if __name__ == "__main__":
    main()
