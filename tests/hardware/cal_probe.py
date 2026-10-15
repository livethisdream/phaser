#!/usr/bin/env python3
"""Diagnose why Calibrate aborts at the array-contrast gate. Read-only.

Runs ON THE PI, next to the backend files, and saves nothing: no calibration
file is written and the array is left at full gain, so a failed run costs
nothing but time.

Measured on kit phaser-00, 2026-10-15, with the HB100 at 10.40763 GHz:

    phaser_find_hb100_headless.py   rx_lo 2.0 GHz   peak -4 dBFS, 75 dB SNR
    phaser_cal_headless.py          rx_lo 2.2 GHz   on -73.83, off -74.76 dBFS

Cal is deaf where the search is not, and both of its contrast readings sit on
the noise floor. That rules out the first theory, which was a stale RX buffer:
a buffer queued before the gains changed would make the "off" capture repeat
the LOUD "on" capture, pinning both near -4 dBFS, not both near -74 dBFS.
Nothing reaches the array at all in cal's configuration, and no flush can fix
a signal that was never received.

What is left is the LO. The two scripts differ by their IF -- the search
hardcodes 2.0 GHz, cal takes config.Rx_freq, which is 2.2 GHz on this Pi -- so
cal asks for an LO 200 MHz higher. `phaser_functions.rf_chain_warnings` already
carries a ceiling above which this chain was measured receiving nothing, but
those numbers came off a different kit ("phaser", HB100 at 10.446953 GHz) and
the comment there says outright to treat them as measured-here rather than as
a datasheet limit. This kit's ceiling has never been measured.

So: sweep Rx_freq across both scripts' values and find where phaser-00 goes
deaf. A clean reading at 2.0 that collapses by 2.2 is the whole bug, and the
fix is an IF this chain can reach rather than anything in the capture path.

RESOLVED, and not the way this file guesses. The sweep below moves the LO and
the IF together, so it cannot say which of them the limit is on -- it found
the edge but attributed it to the LO. lo_vs_pluto.py separated them by
parking the Pluto and moving only the LO, and the limit is on the IF: at
rx_lo 2.2 the tone was absent at every LO across the skirt, while at rx_lo
2.0 it tracked in all eleven rows. Read the Rx_freq sweep below as an IF
sweep. phaser_characterize_headless.py is the productised version.

The flush question is still worth answering, just not as the headline, so the
contrast table runs at BOTH IFs. At a working IF it says whether cal would
clear the gate on an LO fix alone, or whether a stale buffer is waiting
underneath. Note CN0566.SDR_init already forces kernel buffers to 1, so only
the destroy column is really in question -- PR #29 measured that one read
behind persists even at a count of 1.

Usage, on the Pi, from the backend directory:

    python3 cal_probe.py                       # stored HB100 frequency
    python3 cal_probe.py --signal-freq 10.40763e9
"""

import argparse
import sys
import time

import numpy as np

from phaser_functions import (
    MIN_ARRAY_CONTRAST_DB,
    load_hb100_cal,
    lo_chain,
    rf_chain_warnings,
    spec_est,
)

from adi import ad9361
from adi.cn0566 import CN0566

try:
    import config
except ImportError:
    print("Make sure config.py is in this directory")
    sys.exit(1)

AVERAGES = 4
MAX_GAIN = 127


def connect():
    """Same two-step connect as phaser_cal_headless.py, so we probe its path."""
    try:
        print("Connecting to CN0566 via ip:localhost...")
        phaser = CN0566(uri="ip:localhost")
        sdr = ad9361(uri="ip:192.168.2.1")
    except Exception as e:
        print(f"  local connection failed: {e}")
        print("Connecting via ip:phaser.local...")
        phaser = CN0566(uri="ip:phaser.local")
        sdr = ad9361(uri="ip:phaser.local:50901")
    phaser.sdr = sdr
    time.sleep(0.5)
    return phaser, sdr


def connect_and_setup(signal_freq, attempts=3):
    """Connect and configure, retrying a broken pipe the way cal does.

    CN0566.SDR_init writes the AD9361 "initialize" debug attr, which resets
    the part and drops the iiod connection underneath us. The first attempt
    after the headless service exits loses the race often enough that both
    shipping scripts retry it; a probe that does not would just look broken.
    """
    for attempt in range(attempts):
        try:
            phaser, sdr = connect()
            setup(phaser, sdr, signal_freq)
            return phaser, sdr
        except BrokenPipeError as e:
            print(f"  broken pipe on attempt {attempt + 1}/{attempts}: {e}")
            if attempt == attempts - 1:
                raise
            time.sleep(2.0)


def setup(phaser, sdr, signal_freq):
    """Exactly what phaser_cal_headless.py does, up to the contrast check."""
    phaser.configure(device_mode="rx")
    phaser.SDR_init(30000000, config.Tx_freq, config.Rx_freq, 6, -6, 1024)

    phaser.load_channel_cal()
    sdr.rx_hardwaregain_chan0 = sdr.rx_hardwaregain_chan0 + phaser.ccal[0]
    sdr.rx_hardwaregain_chan1 = sdr.rx_hardwaregain_chan1 + phaser.ccal[1]

    phaser.SignalFreq = signal_freq
    sdr.tx_hardwaregain_chan0 = int(-88)
    sdr.tx_hardwaregain_chan1 = int(-88)
    sdr.tx_lo = int(1.0e9)

    phaser.frequency = (int(signal_freq) + config.Rx_freq) // 4
    phaser.freq_dev_step = 5690
    phaser.freq_dev_range = 0
    phaser.freq_dev_time = 0
    phaser.powerdown = 0
    phaser.ramp_mode = "disabled"

    set_all_gains(phaser, MAX_GAIN)
    phaser.Averages = 4


def set_all_gains(phaser, gain):
    for chan in range(8):
        phaser.set_chan_gain(chan, gain, apply_cal=False)


def set_kernel_buffers(sdr, count):
    """Returns True if the count was actually applied."""
    try:
        sdr.rx_destroy_buffer()
    except Exception:
        pass
    try:
        sdr._rxadc.set_kernel_buffers_count(count)
        return True
    except Exception as e:
        print(f"  could not set kernel buffers to {count}: {e}")
        return False


def retune(phaser, sdr, signal_freq, rx_freq):
    """Move the IF: the Pluto LO and the ADF4159 register must move together.

    LO = SignalFreq + Rx_freq and the register takes a quarter of it, so
    changing one without the other walks the tone straight out of the window
    and would look exactly like the deafness we are chasing.
    """
    chain = lo_chain(signal_freq, rx_freq)
    try:
        sdr.rx_destroy_buffer()
    except Exception:
        pass
    sdr.rx_lo = int(rx_freq)
    phaser.frequency = int(chain["adf4159_hz"])
    time.sleep(0.2)
    return chain


def capture_peaks(sdr, destroy, averages=AVERAGES):
    """Per-capture peak dBFS. `destroy` flushes the buffer before each read.

    Returns every peak rather than the mean: production averages four, so if
    only the first is stale the mean hides it and the list does not.
    """
    peaks = []
    spectrum = None
    freqs = None
    for _ in range(averages):
        if destroy:
            try:
                sdr.rx_destroy_buffer()
            except Exception:
                pass
        raw = sdr.rx()
        data = np.asarray(raw[0]) + np.asarray(raw[1])
        ampl, f = spec_est(data, int(sdr.sample_rate), ref=2 ** 11, plot=False)
        spectrum = np.fft.fftshift(ampl)
        freqs = np.fft.fftshift(f)
        peaks.append(float(np.max(spectrum)))
    return peaks, spectrum, freqs


def contrast(phaser, sdr, destroy):
    """(on_peaks, off_peaks, on_spectrum, freqs) for all elements on vs off."""
    set_all_gains(phaser, MAX_GAIN)
    on_peaks, on_spec, freqs = capture_peaks(sdr, destroy)
    set_all_gains(phaser, 0)
    off_peaks, _, _ = capture_peaks(sdr, destroy)
    set_all_gains(phaser, MAX_GAIN)
    return on_peaks, off_peaks, on_spec, freqs


def fmt(peaks):
    return " ".join("%7.2f" % p for p in peaks)


def report_radio(sdr):
    print("\n--- What the radio reports ---")
    for attr in ("sample_rate", "rx_lo", "rx_rf_bandwidth", "rx_buffer_size",
                 "rx_hardwaregain_chan0", "rx_hardwaregain_chan1",
                 "gain_control_mode_chan0"):
        try:
            print("  %-24s %s" % (attr, getattr(sdr, attr)))
        except Exception as e:
            print("  %-24s <unreadable: %s>" % (attr, e))
    try:
        print("  %-24s %s" % ("filter", sdr.filter))
    except Exception:
        pass


def probe_flush(phaser, sdr, label):
    """Contrast under each flush strategy, at whatever IF is currently set."""
    print("\n=== Flush strategies (%s) ===" % label)
    print("Contrast is on - off. The gate needs %.1f dB." % MIN_ARRAY_CONTRAST_DB)
    print()
    print("  %-22s %8s %8s %9s   %s" %
          ("strategy", "on", "off", "contrast", "per-capture peaks (on | off)"))

    results = {}
    for kbuf in (None, 1):
        if kbuf is not None and not set_kernel_buffers(sdr, kbuf):
            continue
        for destroy in (False, True):
            label = "%s + %s" % (
                "kbuf=1" if kbuf == 1 else "kbuf=default",
                "destroy" if destroy else "no flush",
            )
            on, off, _, _ = contrast(phaser, sdr, destroy)
            c = float(np.mean(on) - np.mean(off))
            results[label] = c
            print("  %-22s %8.2f %8.2f %9.2f   %s | %s" %
                  (label, np.mean(on), np.mean(off), c, fmt(on), fmt(off)))
    return results


def probe_window(phaser, sdr, signal_freq, label):
    """Is the loudest thing in the capture the tone, or a spur somewhere else?

    The gate reads a whole-spectrum maximum. With the array on, a peak sitting
    far from DC is a spur, and that changes the diagnosis completely -- so
    print where the peak actually is, not just how big it is.
    """
    print("\n=== Where the peak sits -- array on, buffer flushed (%s) ===" % label)
    set_all_gains(phaser, MAX_GAIN)
    peaks, spec, freqs = capture_peaks(sdr, destroy=True)
    idx = int(np.argmax(spec))
    noise = float(np.median(spec))
    print("  peak      %.2f dBFS at %+0.3f MHz baseband" % (spec[idx], freqs[idx] / 1e6))
    print("  median    %.2f dBFS  (peak is %.1f dB above it)" % (noise, spec[idx] - noise))
    print("  expecting the HB100 near 0 MHz baseband, LO = SignalFreq + Rx_freq")
    print("  signal freq in use: %.6f GHz" % (signal_freq / 1e9))


def probe_lo(phaser, sdr, signal_freq):
    """Find this kit's LO cliff by stepping the IF across both scripts' values.

    The search runs at 2.0 GHz and works; cal runs at 2.2 and does not. 25 MHz
    steps are fine enough to say where in between that happens, which is the
    difference between "raise the ceiling a little" and "2.0 is the only IF
    this kit has".
    """
    print("\n=== Rx_freq sweep (LO ceiling on THIS kit) ===")
    print("  the HB100 search hardcodes 2.00 GHz; cal uses %.2f GHz (config.py)."
          % (config.Rx_freq / 1e9))
    print("  SNR is the peak over the median bin, so it survives a gain change.")
    print()
    print("  %8s %10s %12s %9s %8s" %
          ("Rx_freq", "LO (GHz)", "ADF4159(GHz)", "peak", "SNR"))

    original = int(sdr.rx_lo)
    set_all_gains(phaser, MAX_GAIN)
    rows = []
    try:
        for rx_freq in np.arange(2.0e9, 2.525e9, 25e6):
            chain = retune(phaser, sdr, signal_freq, rx_freq)
            peaks, spec, _ = capture_peaks(sdr, destroy=True, averages=2)
            peak = float(np.mean(peaks))
            snr = peak - float(np.median(spec))
            rows.append((rx_freq, peak, snr))
            print("  %8.3f %10.3f %12.4f %9.2f %8.1f" %
                  (rx_freq / 1e9, chain["lo_hz"] / 1e9,
                   chain["adf4159_hz"] / 1e9, peak, snr))
    finally:
        retune(phaser, sdr, signal_freq, original)
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--signal-freq", type=float, default=None,
                    help="HB100 frequency in Hz; default is the stored cal")
    ap.add_argument("--skip-lo-sweep", action="store_true",
                    help="skip the Rx_freq sweep (it retunes the radio)")
    args = ap.parse_args()

    if args.signal_freq is not None:
        signal_freq = args.signal_freq
        print("Signal frequency from the command line: %.6f GHz" % (signal_freq / 1e9))
    else:
        try:
            signal_freq = load_hb100_cal()
            print("Stored HB100 frequency: %.6f GHz" % (signal_freq / 1e9))
        except Exception:
            signal_freq = config.SignalFreq
            print("No stored HB100 frequency, using config: %.6f GHz"
                  % (signal_freq / 1e9))

    for w in rf_chain_warnings(signal_freq, config.Rx_freq):
        print("  WARNING: %s" % w)

    phaser, sdr = connect_and_setup(signal_freq)
    report_radio(sdr)

    # Cal's own IF first, so the run reproduces the reported failure before it
    # explains it.
    cal_label = "Rx_freq %.2f GHz, as cal runs" % (config.Rx_freq / 1e9)
    retune(phaser, sdr, signal_freq, config.Rx_freq)
    probe_window(phaser, sdr, signal_freq, cal_label)
    probe_flush(phaser, sdr, cal_label)

    if not args.skip_lo_sweep:
        probe_lo(phaser, sdr, signal_freq)

    # Then the IF the search is known to hear on. If the contrast clears
    # MIN_ARRAY_CONTRAST_DB here, an LO fix alone unblocks calibration; if it
    # does not, something in the capture path is wrong as well and the flush
    # columns say whether it is staleness.
    search_label = "Rx_freq 2.00 GHz, as the HB100 search runs"
    retune(phaser, sdr, signal_freq, 2.0e9)
    probe_window(phaser, sdr, signal_freq, search_label)
    probe_flush(phaser, sdr, search_label)

    retune(phaser, sdr, signal_freq, config.Rx_freq)
    set_all_gains(phaser, MAX_GAIN)
    print("\nDone. Nothing was saved.")


if __name__ == "__main__":
    main()
