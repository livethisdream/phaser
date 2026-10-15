#!/usr/bin/env python3
"""Is the cliff in the CN0566 LO chain, or in the Pluto at 2.2 GHz?

LO and Rx_freq move rigidly together when you tune normally -- LO = SignalFreq
+ Rx_freq -- so nothing measured so far separates "the LO cannot go above
12.59" from "the AD9361 cannot hear at 2.2". They imply different fixes: an LO
limit makes the right IF depend on the kit's HB100 frequency, an AD9361 limit
makes it a constant.

They come apart if you stop tuning normally. The tone lands at baseband

    offset = (LO - SignalFreq) - rx_lo

so holding rx_lo FIXED and moving only the CN0566 LO walks the tone across
baseband while the Pluto never retunes. Ten MHz of LO fits inside the ~19.4
MHz the analog filter passes, and the skirt measured earlier (12.598 GHz
strong, 12.608 GHz dead) fits inside ten MHz.

  - cliff in the LO chain -> at rx_lo 2.2 the tone collapses across the sweep
    even though the Pluto is parked;
  - cliff in the Pluto    -> at rx_lo 2.2 everything is dead whatever the LO,
    flat, with no tone to track.

The same sweep at rx_lo 2.0, where everything is healthy, is the control: it
measures the filter rolloff alone, so only a DIFFERENCE in shape counts.
"""

import time

import numpy as np

from phaser_functions import load_hb100_cal, spec_est

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
    """(peak dBFS, baseband freq of the peak, median dBFS), buffer flushed."""
    peaks, where, spec = [], [], None
    for _ in range(averages):
        try:
            sdr.rx_destroy_buffer()
        except Exception:
            pass
        raw = sdr.rx()
        ampl, freqs = spec_est(np.asarray(raw[0]) + np.asarray(raw[1]),
                               int(sdr.sample_rate), ref=2 ** 11, plot=False)
        spec = np.fft.fftshift(ampl)
        f = np.fft.fftshift(freqs)
        idx = int(np.argmax(spec))
        peaks.append(float(spec[idx]))
        where.append(float(f[idx]))
    return float(np.mean(peaks)), float(np.mean(where)), float(np.median(spec))


def sweep_lo_only(phaser, sdr, signal_freq, rx_lo, lo_values, title):
    """Park the Pluto at rx_lo, move only the CN0566 LO."""
    print("\n=== %s ===" % title)
    print("  Pluto rx_lo parked at %.4f GHz for every row below." % (rx_lo / 1e9))
    print("  %10s %12s %11s %9s %8s %11s" %
          ("LO (GHz)", "ADF4159", "expect bb", "peak", "SNR", "peak at bb"))
    try:
        sdr.rx_destroy_buffer()
    except Exception:
        pass
    sdr.rx_lo = int(rx_lo)
    time.sleep(0.2)

    rows = []
    for lo in lo_values:
        try:
            sdr.rx_destroy_buffer()
        except Exception:
            pass
        phaser.frequency = int(lo / 4.0)
        time.sleep(0.25)
        expect = (lo - signal_freq) - rx_lo
        peak, at, med = measure(sdr)
        snr = peak - med
        # Only trust the peak as "the tone" if it is where the arithmetic says.
        tracked = abs(at - expect) < 1.0e6
        rows.append((lo, peak, snr, tracked))
        print("  %10.4f %12.5f %+10.2fM %9.2f %8.1f %+10.2fM%s" %
              (lo / 1e9, lo / 4.0 / 1e9, expect / 1e6, peak, snr, at / 1e6,
               "" if tracked else "   <- not the tone"))
    return rows


def verdict(control, suspect):
    print("\n=== Verdict ===")
    c_span = max(r[1] for r in control) - min(r[1] for r in control)
    s_span = max(r[1] for r in suspect) - min(r[1] for r in suspect)
    c_tracked = sum(1 for r in control if r[3])
    s_tracked = sum(1 for r in suspect if r[3])
    print("  control (rx_lo 2.0): peak varies %.1f dB, tone tracked in %d/%d rows"
          % (c_span, c_tracked, len(control)))
    print("  suspect (rx_lo 2.2): peak varies %.1f dB, tone tracked in %d/%d rows"
          % (s_span, s_tracked, len(suspect)))
    print()
    if s_tracked > 0 and s_span > 20.0:
        print("  -> LO CHAIN. The Pluto never moved, yet the tone was received")
        print("     at the low LOs and lost at the high ones. The usable window")
        print("     is a property of the LO, so the right IF depends on the")
        print("     kit's HB100 frequency and must be derived, not fixed.")
    elif s_tracked == 0:
        print("  -> PLUTO (or something else fixed at rx_lo 2.2). Nothing was")
        print("     received at any LO while parked at 2.2, so the LO was never")
        print("     the variable. The usable window is in IF directly.")
    else:
        print("  -> INCONCLUSIVE. Compare the two tables by hand.")


def main():
    signal_freq = load_hb100_cal()
    print("HB100: %.6f GHz" % (signal_freq / 1e9))
    phaser, sdr = connect_and_setup(signal_freq)

    # Baseband offsets -10 MHz .. 0, inside the ~19.4 MHz analog bandwidth.
    control = sweep_lo_only(
        phaser, sdr, signal_freq, 2.0e9,
        list(np.arange(12.3980e9, 12.4085e9, 1e6)),
        "CONTROL: rx_lo 2.0 GHz (healthy), LO swept +/- the filter only")
    suspect = sweep_lo_only(
        phaser, sdr, signal_freq, 2.2e9,
        list(np.arange(12.5980e9, 12.6085e9, 1e6)),
        "SUSPECT: rx_lo 2.2 GHz (the dead IF), LO swept across the skirt")

    verdict(control, suspect)

    try:
        sdr.rx_destroy_buffer()
    except Exception:
        pass
    sdr.rx_lo = int(2.0e9)
    phaser.frequency = int((signal_freq + 2.0e9) / 4.0)
    print("\nparked back at rx_lo 2.0 GHz. Nothing was saved.")


if __name__ == "__main__":
    main()
