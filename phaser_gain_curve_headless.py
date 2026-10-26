#!/usr/bin/env python3
"""Measure the ADAR1000 rx_gain register's voltage curve. No GUI, no prompts.

WHY THIS EXISTS

The register is not linear in voltage. The ECE 444 Module 3 dry run
(2026-10-23, phaser-00) fitted amplitude = (code/127)**0.68 to three taper peak
drops, and ADAR_pyadi_functions.RX_GAIN_AMPLITUDE ships that fit. This measures
the curve directly, one element at a time, so the fit can be checked and
replaced by a measured table.

It also answers a second question with the same captures: does an element's
PHASE move with its gain setting? Phase calibration runs at code 127, so a
gain-dependent phase would quietly de-calibrate every taper.

HOW

One element under test on, the other three in its sub-array at code 0, and one
reference element in the OTHER sub-array (so in the other Pluto channel) held
at 127. Each sub-array has its own digital channel, so:

    amplitude  = the tone's FFT peak in the test element's channel
    phase      = angle(test channel * conj(reference channel)) at that bin

Both channels share an LO, so the phase difference is stable even though the
HB100 is free-running. The test element's code is stepped 0..127.

Code 0 does not mean silence: an element at 0 still leaks ~2% voltage (-38.8
dBFS residual against -5.25 for the full array, on phaser-00). The floor is
measured and printed, and the fit only uses codes well above it.

SETUP

HB100 at boresight, ~1.2 m. Stop the service so nothing else drives the array,
and pick an Rx gain that puts one element at 127 near -10 dBFS (the script
reports the level and warns outside -10 +/- 3):

    sudo systemctl stop phaser-headless
    python3 phaser_gain_curve_headless.py --rx-gain 30
    python3 phaser_gain_curve_headless.py --rx-gain 30 --elements 1 3 6 8
    sudo systemctl start phaser-headless

Results go to gain_curve.csv (every code, every element) and gain_curve.json
(the fit and a RX_GAIN_AMPLITUDE table to paste into ADAR_pyadi_functions.py,
after which run tools/gen_sim_constants.py).
"""

import argparse
import csv
import json
import math
import sys
import time

import numpy as np

from phaser_functions import (
    _flush_rx,
    _rx_channels,
    load_hb100_cal,
    lo_chain,
    rx_freq_source,
)

try:
    import config
except ImportError:
    print("Make sure config.py is in this directory")
    sys.exit(1)

MAX_GAIN = 127
TARGET_DBFS = -10.0
TARGET_TOL_DB = 3.0
# Codes whose amplitude is within this many dB of the leakage floor are left
# out of the fit: there the leak, at an unknown phase, is a large part of what
# is measured.
FIT_MARGIN_DB = 20.0
# The gain-vs-phase question is asked at this amplitude against full scale.
HALF = 0.5


# --- analysis (pure; tested without hardware) ------------------------------

def tone_at_peak(ch_test, ch_ref):
    """(|test|, angle(test * conj(ref)) in degrees, test dBFS) at test's peak.

    Blackman-windowed, scaled like phaser_functions.spec_est with the 2**11
    reference the rest of the backend uses for dBFS.
    """
    ch_test = np.asarray(ch_test)
    ch_ref = np.asarray(ch_ref)
    window = np.blackman(ch_test.size)
    norm = max(np.sum(window), 1)
    x = np.fft.fft(ch_test * window) / norm
    y = np.fft.fft(ch_ref * window) / norm
    k = int(np.argmax(np.abs(x)))
    mag = float(np.abs(x[k]))
    phase = float(np.degrees(np.angle(x[k] * np.conj(y[k]))))
    dbfs = 20 * math.log10(max(mag, 1e-15) / 2**11)
    return mag, phase, dbfs


def fit_exponent(codes, amplitudes, floor):
    """Least-squares p in amplitude = (code/127)**p, through code 127.

    `amplitudes` are relative to code 127. Codes within FIT_MARGIN_DB of
    `floor` (also relative) are dropped. Returns (p, codes used).
    """
    keep = [
        (c, a) for c, a in zip(codes, amplitudes)
        if 0 < c < MAX_GAIN and a > 0 and a > floor * 10 ** (FIT_MARGIN_DB / 20)
    ]
    if len(keep) < 2:
        return float("nan"), 0
    lx = np.log([c / MAX_GAIN for c, _ in keep])
    ly = np.log([a for _, a in keep])
    return float(np.sum(lx * ly) / np.sum(lx * lx)), len(keep)


def amplitude_table(curves):
    """Mean of several elements' relative curves, as a 128-entry table.

    Each curve maps code -> amplitude relative to its own code 127. Codes not
    measured are interpolated; code 0 is pinned to 0.0 (ADAR_set_Taper sends
    it through the attenuator) and 127 to 1.0. The result is forced
    monotonic, which rx_gain_code's nearest-neighbor inverse assumes.
    """
    grid = np.arange(MAX_GAIN + 1)
    rows = []
    for curve in curves:
        codes = sorted(curve)
        rows.append(np.interp(grid, codes, [curve[c] for c in codes]))
    table = np.mean(rows, axis=0)
    table[0] = 0.0
    table[MAX_GAIN] = 1.0
    table = np.maximum.accumulate(table)
    return [float(round(v, 6)) for v in table]


def code_nearest(curve, amplitude):
    return min(curve, key=lambda c: abs(curve[c] - amplitude))


# --- hardware ---------------------------------------------------------------

def connect():
    """Same two-step connect as the other headless scripts."""
    from adi import ad9361
    from adi.cn0566 import CN0566

    try:
        print("Attempting to connect to CN0566 via ip:localhost...")
        phaser = CN0566(uri="ip:localhost")
        print("Found CN0566. Connecting to PlutoSDR via default IP address...")
        sdr = ad9361(uri="ip:192.168.2.1")
        print("PlutoSDR connected.")
    except Exception as e:
        print(f"Local connection failed: {e}")
        print("CN0566 on ip:localhost not found, connecting via ip:phaser.local...")
        phaser = CN0566(uri="ip:phaser.local")
        print("Found CN0566. Connecting to PlutoSDR via shared context...")
        sdr = ad9361(uri="ip:phaser.local:50901")
        print("Found SDR on shared phaser.local.")
    phaser.sdr = sdr
    time.sleep(0.5)
    return phaser, sdr


def setup(phaser, sdr, signal_freq, rx_freq, rx_gain):
    """Configure as calibration does, at this kit's IF and the given Rx gain."""
    phaser.configure(device_mode="rx")
    phaser.SDR_init(30000000, config.Tx_freq, rx_freq, rx_gain, -6, 1024)

    phaser.SignalFreq = signal_freq
    sdr.tx_hardwaregain_chan0 = int(-88)
    sdr.tx_hardwaregain_chan1 = int(-88)
    sdr.tx_lo = int(1.0e9)

    phaser.frequency = int(lo_chain(signal_freq, rx_freq)["adf4159_hz"])
    phaser.freq_dev_step = 5690
    phaser.freq_dev_range = 0
    phaser.freq_dev_time = 0
    phaser.powerdown = 0
    phaser.ramp_mode = "disabled"

    for chan in range(8):
        phaser.set_chan_phase(chan, 0, apply_cal=False)


def set_gains(phaser, gains):
    for chan, code in enumerate(gains):
        phaser.set_chan_gain(chan, int(code), apply_cal=False)


def capture(phaser, averages):
    """`averages` fresh captures, each (ch0 samples, ch1 samples)."""
    _flush_rx(phaser.sdr)
    return [_rx_channels(phaser) for _ in range(max(1, averages))]


def measure(phaser, test_ch, ref_ch, averages):
    """Average tone_at_peak over captures, with the test channel's samples."""
    mags, phasors = [], []
    for pair in capture(phaser, averages):
        mag, phase, _ = tone_at_peak(pair[test_ch], pair[ref_ch])
        mags.append(mag)
        phasors.append(np.exp(1j * np.radians(phase)))
    return (float(np.mean(mags)),
            float(np.degrees(np.angle(np.mean(phasors)))),
            float(20 * np.log10(max(np.mean(mags), 1e-15) / 2**11)))


def channel_map(phaser, averages):
    """Which Pluto channel (0/1) each element lands in, measured, not assumed."""
    mapping = []
    for elem in range(8):
        set_gains(phaser, [MAX_GAIN if c == elem else 0 for c in range(8)])
        pairs = capture(phaser, averages)
        p0 = np.mean([tone_at_peak(a, b)[0] for a, b in pairs])
        p1 = np.mean([tone_at_peak(b, a)[0] for a, b in pairs])
        mapping.append(0 if p0 >= p1 else 1)
    return mapping


def run(args):
    if args.signal_freq is not None:
        signal_freq = args.signal_freq
    else:
        try:
            signal_freq = load_hb100_cal()
        except Exception:
            print("No stored HB100 frequency. Run phaser_find_hb100_headless.py"
                  " first, or pass --signal-freq.")
            return 1
    chosen = rx_freq_source(config)
    rx_freq = chosen["rx_freq_hz"]
    print("HB100 %.6f GHz, IF %.3f GHz (%s), Rx gain %d dB"
          % (signal_freq / 1e9, rx_freq / 1e9, chosen["source"], args.rx_gain))

    phaser, sdr = connect()
    setup(phaser, sdr, signal_freq, rx_freq, args.rx_gain)

    chan_of = channel_map(phaser, args.averages)
    print("Element -> Pluto channel: %s"
          % ", ".join("E%d:%d" % (e + 1, c) for e, c in enumerate(chan_of)))
    if len(set(chan_of)) < 2:
        print("ERROR: every element landed in one channel; no reference is"
              " possible. Is the HB100 aimed at the array?")
        return 1

    codes = sorted(set(list(range(0, MAX_GAIN + 1, args.step)) + [MAX_GAIN]))
    rows, curves, summary = [], [], {}

    for elem in [e - 1 for e in args.elements]:
        test_ch = chan_of[elem]
        ref_ch = 1 - test_ch
        ref = next(e for e in range(8) if chan_of[e] == ref_ch)
        base = [0] * 8
        base[ref] = MAX_GAIN

        set_gains(phaser, base)
        floor_mag, _, floor_dbfs = measure(phaser, test_ch, ref_ch, args.averages)

        print("\n=== E%d (channel %d), reference E%d ===" % (elem + 1, test_ch, ref + 1))
        print("  leakage floor, E%d at 0: %.1f dBFS" % (elem + 1, floor_dbfs))
        print("  %5s %9s %9s %9s" % ("code", "dBFS", "rel dB", "phase"))
        meas = {}
        for code in codes:
            gains = list(base)
            gains[elem] = code
            set_gains(phaser, gains)
            meas[code] = measure(phaser, test_ch, ref_ch, args.averages)

        full_mag, full_phase, full_dbfs = meas[MAX_GAIN]
        if abs(full_dbfs - TARGET_DBFS) > TARGET_TOL_DB:
            print("  WARNING: code 127 reads %.1f dBFS, outside %.0f +/- %.0f."
                  " Adjust --rx-gain and re-run." % (full_dbfs, TARGET_DBFS, TARGET_TOL_DB))
        curve = {}
        for code in codes:
            mag, phase, dbfs = meas[code]
            rel = mag / full_mag
            dphase = (phase - full_phase + 180) % 360 - 180
            curve[code] = rel
            rows.append({"element": elem + 1, "code": code, "dbfs": round(dbfs, 3),
                         "amplitude_rel": round(rel, 6),
                         "phase_rel_deg": round(dphase, 2)})
            print("  %5d %9.2f %9.2f %9.2f"
                  % (code, dbfs, 20 * math.log10(max(rel, 1e-15)), dphase))

        p, used = fit_exponent(codes, [curve[c] for c in codes], floor_mag / full_mag)
        half_code = code_nearest(curve, HALF)
        half_phase = next(r["phase_rel_deg"] for r in rows
                          if r["element"] == elem + 1 and r["code"] == half_code)
        summary["E%d" % (elem + 1)] = {
            "channel": test_ch, "reference": ref + 1,
            "full_scale_dbfs": round(full_dbfs, 2),
            "floor_dbfs": round(floor_dbfs, 2),
            "exponent": round(p, 4), "fit_points": used,
            "half_amplitude_code": half_code,
            "phase_at_half_vs_full_deg": half_phase,
        }
        print("  fitted exponent %.3f over %d codes;  amplitude 0.5 at code %d,"
              " phase there %+.1f deg vs code 127" % (p, used, half_code, half_phase))
        curves.append(curve)

    table = amplitude_table(curves)
    # How far apart the elements' curves are: max |dB| from the mean table at
    # codes above the fit floor. One curve for all eight needs this small.
    spread = 0.0
    for curve in curves:
        for c, a in curve.items():
            if c and table[c] > 0.05 and a > 0:
                spread = max(spread, abs(20 * math.log10(a / table[c])))
    exps = [s["exponent"] for s in summary.values()]

    print("\n=== Result ===")
    for name, s in summary.items():
        print("  %s  exponent %.3f   phase at 0.5 vs 1.0: %+.1f deg"
              % (name, s["exponent"], s["phase_at_half_vs_full_deg"]))
    print("  max spread between elements' curves: %.2f dB" % spread)
    print("  shipped fit: exponent 0.68.  Measured mean: %.3f" % float(np.mean(exps)))

    with open(args.csv, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    with open(args.json, "w") as fh:
        json.dump({"signal_freq_hz": signal_freq, "rx_freq_hz": rx_freq,
                   "rx_gain_db": args.rx_gain, "elements": summary,
                   "spread_db": round(spread, 3),
                   "RX_GAIN_AMPLITUDE": table}, fh, indent=1)
    print("  wrote %s and %s" % (args.csv, args.json))
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rx-gain", type=int, default=30,
                    help="Pluto Rx gain, dB; aim for code 127 near -10 dBFS")
    ap.add_argument("--elements", type=int, nargs="+", default=[1, 6],
                    help="elements (1-8) to step; default 1 and 6, one per ADAR1000")
    ap.add_argument("--step", type=int, default=1, help="register code step")
    ap.add_argument("--averages", type=int, default=4)
    ap.add_argument("--signal-freq", type=float, default=None,
                    help="HB100 frequency in Hz; default is the stored value")
    ap.add_argument("--csv", default="gain_curve.csv")
    ap.add_argument("--json", default="gain_curve.json")
    args = ap.parse_args()
    if any(not 1 <= e <= 8 for e in args.elements):
        ap.error("--elements are 1-8")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
