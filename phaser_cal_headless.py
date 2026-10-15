#!/usr/bin/env python3
"""
Headless phaser calibration - no GUI, no prompts.
Performs channel, gain, and phase calibration automatically.

Based on the "cal" option from phaser_examples.py in pyadi-iio.
"""

import sys
import time

from phaser_functions import (
    MIN_ARRAY_CONTRAST_DB,
    MIN_BOOTSTRAP_SNR_DB,
    choose_rx_freq,
    load_if_cal,
    measure_if_window,
    save_if_cal,
    channel_calibration,
    gain_calibration,
    load_hb100_cal,
    lo_chain,
    measure_array_contrast,
    phase_calibration,
    rf_chain_warnings,
    rx_freq_source,
    save_channel_cal,
    save_gain_cal,
    save_phase_cal,
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


def should_characterise(contrast_db, already_tried):
    """Is a recovery sweep the right response to this failed contrast check?

    Three conditions, and the middle one is the load-bearing one. A unit that
    already has a measurement is NOT re-swept: there, a failed gate means
    something else is wrong -- a dead source, a cable -- and sweeping under
    those conditions would overwrite a good measurement with a bad one.
    """
    if contrast_db >= MIN_ARRAY_CONTRAST_DB:
        return False
    if already_tried:
        return False
    return load_if_cal() is None


def characterise_in_place(my_phaser, my_sdr, signal_freq):
    """Measure this unit's IF window using the already-open connection.

    Called only after the contrast gate has failed on a unit that has never
    been characterised. That combination is worth recovering from rather than
    reporting, because the bench conditions characterisation needs -- HB100
    powered and aimed at the array -- are exactly the ones calibration needs,
    so anyone who got this far has already set up everything the sweep
    requires. The usual objection to chaining the two does not apply.

    It is deliberately NOT attempted on a unit that already has a
    measurement. There the gate failing means something else is wrong -- a
    dead source, a cable -- and re-sweeping under those conditions would
    overwrite a good measurement with a bad one.

    Returns the new IF in Hz, or None, having left the radio tuned to
    whatever it decided. Saves nothing when the sweep heard nothing, so a run
    with the HB100 off is safe.
    """
    print()
    print("This unit has never been characterised, and the usable IF differs")
    print("between kits by ~200 MHz. Measuring it now, since the HB100 is")
    print("evidently set up -- about a minute.")
    print()
    print("  %8s %10s %10s %8s" % ("IF(GHz)", "LO(GHz)", "peak", "SNR"))

    rows = measure_if_window(
        my_phaser, my_sdr, signal_freq,
        progress=lambda r: print("  %8.3f %10.3f %10.2f %8.1f"
                                 % (r["rx_freq_hz"] / 1e9, r["lo_hz"] / 1e9,
                                    r["peak_dbfs"], r["snr_db"])))

    best_snr = max((r["snr_db"] for r in rows), default=0.0)
    if best_snr < MIN_BOOTSTRAP_SNR_DB:
        print()
        print("  Nothing was received at ANY IF (best %.1f dB SNR, need %.1f)."
              % (best_snr, MIN_BOOTSTRAP_SNR_DB))
        print("  So the IF was not the problem. Nothing saved.")
        return None

    window = choose_rx_freq(rows)
    if not window.get("ok"):
        print()
        print("  %s" % window.get("reason", "the sweep could not be read"))
        print("  Nothing saved.")
        return None

    save_if_cal(window, signal_freq_hz=signal_freq, rows=rows)
    new_if = float(window["rx_freq_hz"])
    print()
    print("  clean window  %.3f - %.3f GHz, best SNR %.1f dB"
          % (window["clean_min_hz"] / 1e9, window["clean_max_hz"] / 1e9,
             window["best_snr_db"]))
    print("  chosen IF     %.3f GHz  (saved; every later run reads this)"
          % (new_if / 1e9))

    # measure_if_window leaves the radio on the last step of the sweep.
    chain = lo_chain(signal_freq, new_if)
    try:
        my_sdr.rx_destroy_buffer()
    except Exception:
        pass
    my_sdr.rx_lo = int(new_if)
    my_phaser.frequency = int(chain["adf4159_hz"])
    for chan in range(8):
        my_phaser.set_chan_gain(chan, 127, apply_cal=False)
    time.sleep(0.3)
    return new_if


def do_calibration():
    """Run full calibration sequence. Returns True on success."""

    my_phaser = None
    my_sdr = None

    # The IF is per-unit and measured, not a constant: see
    # phaser_characterize_headless.py. Resolved once, before any hardware is
    # touched, so the figure printed is the figure used.
    chosen = rx_freq_source(config)
    rx_freq = chosen["rx_freq_hz"]
    print("IF: %.3f GHz, from %s" % (rx_freq / 1e9, chosen["source"]))
    for line in chosen["warnings"]:
        print("  WARNING: %s" % line)

    # At most one recovery sweep per invocation, however many times the
    # reconnect loop below runs. Measuring twice would say nothing new and
    # would double an already slow failure.
    characterisation_tried = False

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
            my_phaser.SDR_init(30000000, config.Tx_freq, rx_freq, 6, -6, 1024)

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
            my_phaser.frequency = (int(my_phaser.SignalFreq) + int(rx_freq)) // 4
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

            # Preflight: prove the array is actually receiving something before
            # calibrating against it.
            #
            # Every calibration below is an argmax over measured power, and an
            # argmax is meaningless if the power it measures does not depend on
            # the thing being swept. That is exactly what happens when the
            # loudest signal is a spur generated inside the receiver: phase cal
            # returns noise, reruns disagree by hundreds of degrees, and
            # channel_cal can come out negative. All of it saved silently, and
            # the first sign of trouble is a flat beam pattern in the UI.
            #
            # Disabling every element cannot attenuate a spur produced after
            # the mixer, so the on/off difference separates the two cases
            # cleanly and costs two captures.
            print("\n--- Signal Check ---")
            contrast_db, on_dbfs, off_dbfs = measure_array_contrast(my_phaser)
            print(
                "All elements on: %.2f dBFS, all off: %.2f dBFS, contrast: %.2f dB"
                % (on_dbfs, off_dbfs, contrast_db)
            )
            # An uncharacterised unit failing this gate is the one case
            # worth recovering rather than reporting: the commonest cause is
            # an IF this kit cannot hear, and the bench is demonstrably set
            # up, because that is what the operator just did.
            if should_characterise(contrast_db, characterisation_tried):
                characterisation_tried = True
                new_if = characterise_in_place(
                    my_phaser, my_sdr, my_phaser.SignalFreq)
                if new_if is not None:
                    rx_freq = new_if
                    chosen = rx_freq_source(config)
                    print("\n--- Signal Check (retry at %.3f GHz) ---"
                          % (rx_freq / 1e9))
                    contrast_db, on_dbfs, off_dbfs = measure_array_contrast(my_phaser)
                    print(
                        "All elements on: %.2f dBFS, all off: %.2f dBFS, "
                        "contrast: %.2f dB" % (on_dbfs, off_dbfs, contrast_db)
                    )
                    if contrast_db >= MIN_ARRAY_CONTRAST_DB:
                        print("Recovered. Continuing with calibration.")

            if contrast_db < MIN_ARRAY_CONTRAST_DB:
                chain = lo_chain(my_phaser.SignalFreq, rx_freq)
                print()
                print("CALIBRATION ABORTED: no signal is reaching the array.")
                print(
                    "Disabling all eight elements changed the received power by "
                    "only %.2f dB (need %.2f dB), so what the receiver hears "
                    "does not come through the antennas."
                    % (contrast_db, MIN_ARRAY_CONTRAST_DB)
                )
                print("Nothing was saved; the previous calibration is intact.")
                print()
                # The chain goes first because it is the only part of this
                # list nobody can check by looking at the bench, and an IF
                # the unit cannot hear produces exactly these numbers.
                print("The chain this ran on:")
                print("  HB100    %.6f GHz" % (my_phaser.SignalFreq / 1e9))
                print("  IF       %.3f GHz      (%s)" % (rx_freq / 1e9, chosen["source"]))
                print("  LO       %.3f GHz      = HB100 + IF" % (chain["lo_hz"] / 1e9))
                print("  ADF4159  %.4f GHz     = LO / 4" % (chain["adf4159_hz"] / 1e9))
                for line in rf_chain_warnings(my_phaser.SignalFreq, rx_freq):
                    print("  WARNING: %s" % line)
                print()
                print("Check, in this order:")
                if characterisation_tried:
                    # Already swept every IF and heard nothing anywhere, so
                    # the IF is ruled out and saying "characterise it" would
                    # send someone round the same loop.
                    print("  1. the HB100 is powered and pointed at the array.")
                    print("     The IF is already ruled out: a sweep of every")
                    print("     IF just now heard nothing at any of them, so")
                    print("     no tuning makes this array hear.")
                    print("  2. the RF cables from the CN0566 to the Pluto Rx")
                    print("     inputs")
                    print("  3. the stored HB100 frequency -- re-run the HB100")
                    print("     search")
                    return False
                print("  1. whether this unit can hear at that IF at all. The")
                print("     usable IF differs by ~200 MHz between kits, and an")
                print("     IF outside it reads exactly like a dead source.")
                print("     Run phaser_characterize_headless.py to measure it.")
                print("  2. the HB100 is powered and pointed at the array")
                print("  3. the stored HB100 frequency is right -- re-run the")
                print("     HB100 search (a frequency error of more than about")
                print("     1.5 MHz puts the tone outside the analysis window)")
                print("  4. the RF cables from the CN0566 to the Pluto Rx inputs")
                return False

            # Channel calibration (same as phaser_examples.py)
            print("\n--- Channel Calibration ---")
            my_phaser.set_beam_phase_diff(0.0)
            channel_calibration(my_phaser, verbose=True)
            save_channel_cal(my_phaser.ccal)
            print(f"Channel calibration saved: {my_phaser.ccal}")

            # Gain calibration (same as phaser_examples.py)
            print("\n--- Gain Calibration ---")
            my_phaser.set_beam_phase_diff(0.0)
            gain_calibration(my_phaser, verbose=True)
            save_gain_cal(my_phaser.gcal)
            print(f"Gain calibration saved: {my_phaser.gcal}")

            # Phase calibration (same as phaser_examples.py)
            print("\n--- Phase Calibration ---")
            phase_calibration(my_phaser, verbose=True)
            save_phase_cal(my_phaser.pcal)
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
