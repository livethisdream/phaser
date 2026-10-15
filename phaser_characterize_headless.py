#!/usr/bin/env python3
"""Measure this unit's usable IF window and store it. No GUI, no prompts.

WHY THIS EXISTS

The IF that works is a property of the individual kit, not of the design. Two
kits measured so far differ by about 200 MHz in where reception dies, and
config.py ships a value (2.2 GHz) that one of them can hear and the other
cannot. Because config.py is site-owned and deliberately never deployed over,
a kit that works is a kit somebody hand-edited, and that edit cannot travel.

The failure is silent in every direction. The attribute write is accepted, the
readback agrees, MUXOUT is not wired as lock detect on this board, and the
calibration that runs next reports "no signal is reaching the array" -- which
reads as a dead HB100 or an unplugged cable, not a configuration that cannot
hear. So the only defence is to measure each unit and write the answer where
deployment can see it.

WHAT IT MEASURES

The IF is stepped across IF_SWEEP_START_HZ..IF_SWEEP_STOP_HZ with the LO
tracking it, and the received peak recorded at each step. The chosen IF is the
middle of the longest clean run -- the middle specifically, because the edge
is the thing that varies between units, and the middle of what this unit does
is the only choice not borrowed from another kit.

ORDER OF OPERATIONS

    1. phaser_find_hb100_headless.py     find the source
    2. phaser_characterize_headless.py   this: find the IF  <-- once per kit
    3. phaser_cal_headless.py            channel, gain, phase

Step 1 first because the sweep needs a tone of known frequency to measure.
Step 2 once per kit, on a bench with the HB100 aimed at the array; the result
is stored in calibration.json and every later run reads it from there.

Usage, on the Pi, from the backend directory:

    python3 phaser_characterize_headless.py
    python3 phaser_characterize_headless.py --dry-run
    python3 phaser_characterize_headless.py --signal-freq 10.40763e9
"""

import argparse
import sys
import time

from phaser_functions import (
    IF_SWEEP_START_HZ,
    MIN_BOOTSTRAP_SNR_DB,
    IF_SWEEP_STOP_HZ,
    IF_SWEEP_STEP_HZ,
    DEFAULT_RX_FREQ_HZ,
    choose_rx_freq,
    load_hb100_cal,
    load_if_cal,
    lo_chain,
    measure_if_window,
    save_if_cal,
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
MAX_GAIN = 127


def connect():
    """Same two-step connect as the other headless scripts."""
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


def setup(phaser, sdr, signal_freq, bootstrap_if):
    """Configure exactly as calibration does, at the bootstrap IF.

    Matching calibration matters: this measures the window THAT path sees, so
    a difference in filter, sample rate or gain here would characterise a
    configuration nobody runs.
    """
    phaser.configure(device_mode="rx")
    phaser.SDR_init(30000000, config.Tx_freq, bootstrap_if, 6, -6, 1024)

    phaser.SignalFreq = signal_freq
    sdr.tx_hardwaregain_chan0 = int(-88)
    sdr.tx_hardwaregain_chan1 = int(-88)
    sdr.tx_lo = int(1.0e9)

    phaser.frequency = int(lo_chain(signal_freq, bootstrap_if)["adf4159_hz"])
    phaser.freq_dev_step = 5690
    phaser.freq_dev_range = 0
    phaser.freq_dev_time = 0
    phaser.powerdown = 0
    phaser.ramp_mode = "disabled"

    for chan in range(8):
        phaser.set_chan_gain(chan, MAX_GAIN, apply_cal=False)
        phaser.set_chan_phase(chan, 0, apply_cal=False)
    phaser.Averages = 4


def print_row(row):
    print("  %8.3f %10.3f %10.2f %8.1f"
          % (row["rx_freq_hz"] / 1e9, row["lo_hz"] / 1e9,
             row["peak_dbfs"], row["snr_db"]))


def report_failure(window, rows):
    print()
    print("CHARACTERISATION FAILED: %s" % window.get("reason", "unknown"))
    print()
    best = max(rows, key=lambda r: r["snr_db"]) if rows else None
    if best is not None:
        print("The best step was %.3f GHz at %.1f dB SNR (%.2f dBFS)."
              % (best["rx_freq_hz"] / 1e9, best["snr_db"], best["peak_dbfs"]))
    print("Nothing was saved; any previous characterisation is intact.")
    print()
    print("Check, in this order:")
    print("  1. the HB100 is powered and aimed at the array, at boresight")
    print("  2. the stored HB100 frequency is current -- re-run")
    print("     phaser_find_hb100_headless.py")
    print("  3. the RF cables from the CN0566 to the Pluto Rx inputs")


def characterize(signal_freq, bootstrap_if, dry_run=False):
    """Run the sweep and store the result. Returns True on success."""
    phaser = None
    sdr = None

    for attempt in range(MAX_RETRIES):
        try:
            print(f"Characterisation attempt {attempt + 1}/{MAX_RETRIES}...")
            phaser, sdr = connect()
            setup(phaser, sdr, signal_freq, bootstrap_if)

            print("\n=== Sweeping the IF ===")
            print("HB100 at %.6f GHz; the LO tracks the IF so the tone stays"
                  % (signal_freq / 1e9))
            print("in the analysis window at every step.")
            print()
            print("  %8s %10s %10s %8s" % ("IF(GHz)", "LO(GHz)", "peak", "SNR"))

            rows = measure_if_window(
                phaser, sdr, signal_freq,
                start_hz=IF_SWEEP_START_HZ, stop_hz=IF_SWEEP_STOP_HZ,
                step_hz=IF_SWEEP_STEP_HZ, progress=print_row)

            best_snr = max((r["snr_db"] for r in rows), default=0.0)
            if best_snr < MIN_BOOTSTRAP_SNR_DB:
                report_failure(
                    {"reason": "nothing was received at any IF (best %.1f dB "
                               "SNR, need %.1f)"
                               % (best_snr, MIN_BOOTSTRAP_SNR_DB)}, rows)
                return False

            window = choose_rx_freq(rows)
            if not window.get("ok"):
                report_failure(window, rows)
                return False

            print("\n=== Result ===")
            print("  clean window   %.3f - %.3f GHz  (%.0f MHz wide, %d of %d steps)"
                  % (window["clean_min_hz"] / 1e9, window["clean_max_hz"] / 1e9,
                     window["span_hz"] / 1e6, window["clean_steps"],
                     window["swept_steps"]))
            print("  best SNR       %.1f dB" % window["best_snr_db"])
            print("  chosen IF      %.3f GHz   (middle of the clean run)"
                  % (window["rx_freq_hz"] / 1e9))
            print("  LO it implies  %.3f GHz   = HB100 + IF"
                  % (lo_chain(signal_freq, window["rx_freq_hz"])["lo_hz"] / 1e9))

            # Either edge can fall outside the sweep, and then the "middle"
            # is the middle of what was looked at rather than of the window.
            # Say so: the number is still inside measured-clean range, but
            # its margin to the real edge is unknown on that side.
            if window["clean_max_hz"] >= IF_SWEEP_STOP_HZ - 1.0:
                print()
                print("  NOTE: the clean run reaches the top of the sweep, so the")
                print("        upper edge is beyond %.2f GHz and was not found."
                      % (IF_SWEEP_STOP_HZ / 1e9))
                print("        The chosen IF is biased low by that, which is the")
                print("        safe direction -- the top edge is the one that bites.")
            if window["clean_min_hz"] <= IF_SWEEP_START_HZ + 1.0:
                print()
                print("  NOTE: the clean run reaches the bottom of the sweep, so the")
                print("        lower edge is below %.2f GHz and was not found."
                      % (IF_SWEEP_START_HZ / 1e9))
                print("        The chosen IF is biased high by that. Margin to the")
                print("        upper edge is %.0f MHz, against the ~200 MHz by which"
                      % ((window["clean_max_hz"] - window["rx_freq_hz"]) / 1e6))
                print("        the two kits measured so far differ.")

            if dry_run:
                print("\n--dry-run: nothing written.")
                return True

            save_if_cal(window, signal_freq_hz=signal_freq, rows=rows)
            print("\nSaved to calibration.json. Calibration and the backend")
            print("will use %.3f GHz from now on, on this unit only."
                  % (window["rx_freq_hz"] / 1e9))
            return True

        except BrokenPipeError as e:
            # SDR_init resets the AD9361 and drops the context underneath us;
            # the first attempt after the service exits loses that race often.
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
            try:
                if sdr is not None and hasattr(sdr, "rx_destroy_buffer"):
                    sdr.rx_destroy_buffer()
            except Exception:
                pass

    return False


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--signal-freq", type=float, default=None,
                    help="HB100 frequency in Hz; default is the stored value")
    ap.add_argument("--bootstrap-if", type=float, default=None,
                    help="IF to configure before sweeping (Hz); default %.2f GHz"
                         % (DEFAULT_RX_FREQ_HZ / 1e9))
    ap.add_argument("--dry-run", action="store_true",
                    help="measure and print, but do not write calibration.json")
    args = ap.parse_args()

    if args.signal_freq is not None:
        signal_freq = args.signal_freq
        print("HB100 from the command line: %.6f GHz" % (signal_freq / 1e9))
    else:
        try:
            signal_freq = load_hb100_cal()
        except Exception:
            print("No stored HB100 frequency.")
            print("Run phaser_find_hb100_headless.py first -- the sweep needs a")
            print("tone of known frequency to measure against.")
            return 1
        print("HB100 (stored): %.6f GHz" % (signal_freq / 1e9))

    previous = load_if_cal()
    if previous is not None:
        print("Previously characterised at %.3f GHz; re-measuring."
              % (float(previous["rx_freq_hz"]) / 1e9))

    bootstrap = args.bootstrap_if or DEFAULT_RX_FREQ_HZ
    ok = characterize(signal_freq, bootstrap, dry_run=args.dry_run)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
