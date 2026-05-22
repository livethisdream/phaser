#!/usr/bin/env python3
#  Must use Python 3
# Copyright (C) 2022 Analog Devices, Inc.
#
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without modification,
# are permitted provided that the following conditions are met:
#     - Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     - Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in
#       the documentation and/or other materials provided with the
#       distribution.
#     - Neither the name of Analog Devices, Inc. nor the names of its
#       contributors may be used to endorse or promote products derived
#       from this software without specific prior written permission.
#     - The use of this software may or may not infringe the patent rights
#       of one or more patent holders.  This license does not release you
#       from the requirement that you obtain separate licenses from these
#       patent holders to use this software.
#     - Use of the software either in source or binary form, must be run
#       on or directly connected to an Analog Devices Inc. component.
#
# THIS SOFTWARE IS PROVIDED BY ANALOG DEVICES "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES,
# INCLUDING, BUT NOT LIMITED TO, NON-INFRINGEMENT, MERCHANTABILITY AND FITNESS FOR A
# PARTICULAR PURPOSE ARE DISCLAIMED.
#
# IN NO EVENT SHALL ANALOG DEVICES BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, INTELLECTUAL PROPERTY
# RIGHTS, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR
# BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF
# THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.


# Basic utility script for working with the CN0566 "Phaser" board. Accepts the following
# command line arguments:

# plot - plot beam pattern, rectangular element weighting. If cal files are present,
#        they will be loaded.

# cal - perform both gain and phase calibration, save to files.

'''Modified version of "phaser_examples.py" to just run cal.  No need for the "cal" command line argument'''

import sys
import time
import os
import socket

import matplotlib.pyplot as plt
import adi
from phaser_functions import (
    channel_calibration,
    gain_calibration,
    load_hb100_cal,
    phase_calibration,
    save_channel_cal,
    save_gain_cal,
    save_phase_cal,
)

try:
    import config as config
except ImportError:
    print("Make sure config.py is in this directory")
    sys.exit(1)

colors = ["black", "gray", "red", "orange", "yellow", "green", "blue", "purple"]
ENABLE_PLOTS = os.environ.get("PHASER_CAL_PLOT", "0").strip().lower() in ("1", "true", "yes", "on")


def _detect_hostname():
    if socket.gethostname().find(".") >= 0:
        return socket.gethostname()
    return socket.gethostbyaddr(socket.gethostname())[0]


def _auto_uris(hostname):
    if "phaser" in hostname:
        return "ip:localhost", "ip:192.168.2.1"
    # Laptop or other non-board host with context-forwarded SDR access.
    return "ip:phaser.local", "ip:phaser.local:50901"


def resolve_hardware_uris():
    hostname = _detect_hostname()
    auto_rpi_uri, auto_sdr_uri = _auto_uris(hostname)

    env_rpi_uri = os.environ.get("PHASER_RPI_URI")
    env_sdr_uri = os.environ.get("PHASER_SDR_URI")

    cfg_mode = str(getattr(config, "uri_mode", "auto")).strip().lower()
    cfg_rpi_uri = getattr(config, "rpi_uri", None)
    cfg_sdr_uri = getattr(config, "sdr_uri", None)

    # Environment vars are the highest-precedence deployment override.
    if env_rpi_uri and env_sdr_uri:
        return hostname, env_rpi_uri, env_sdr_uri

    if cfg_mode == "custom":
        if not cfg_rpi_uri or not cfg_sdr_uri:
            raise ValueError("uri_mode='custom' requires both config.rpi_uri and config.sdr_uri")
        return hostname, cfg_rpi_uri, cfg_sdr_uri

    if cfg_mode == "prefer_config":
        return hostname, cfg_rpi_uri or auto_rpi_uri, cfg_sdr_uri or auto_sdr_uri

    return hostname, auto_rpi_uri, auto_sdr_uri


def _extract_hostname(uri):
    """Extract hostname from URI format (e.g., 'ip:phaser.local:50901' -> 'phaser.local')."""
    if ":" in uri:
        parts = uri.split(":")
        return parts[1] if len(parts) >= 2 else uri
    return uri


def _test_hostname_resolution(hostname_or_ip, dns_cache=None):
    """Test if hostname resolves to an IP or if IP is valid.

    Args:
        hostname_or_ip: URI or hostname to resolve
        dns_cache: Optional dict to cache/reuse DNS results (avoids Windows mDNS flakiness)

    Returns: (success, result/error, target_hostname_attempted)
    """
    if dns_cache is None:
        dns_cache = {}

    target = _extract_hostname(hostname_or_ip)

    # Check cache first (Windows mDNS can fail on repeated lookups)
    if target in dns_cache:
        cached = dns_cache[target]
        return cached[0], cached[1], target

    try:
        resolved_ip = socket.gethostbyname(target)
        dns_cache[target] = (True, resolved_ip)
        return True, resolved_ip, target
    except socket.gaierror as e:
        dns_cache[target] = (False, str(e))
        return False, str(e), target
    except Exception as e:
        dns_cache[target] = (False, str(e))
        return False, str(e), target


def do_cal_channel():
    my_phaser.set_beam_phase_diff(0.0)
    channel_calibration(my_phaser, verbose=True)


def do_cal_gain():
    my_phaser.set_beam_phase_diff(0.0)
    #    plot_data = my_phaser.gain_calibration(verbose=True)  # Start Gain Calibration
    plot_data = gain_calibration(my_phaser, verbose=True)  # Start Gain Calibration
    if ENABLE_PLOTS:
        plt.figure(4)
        plt.title("Gain calibration FFTs")
        plt.xlabel("FFT Bin number")
        plt.ylabel("Amplitude (ADC counts)")
        for i in range(0, 8):
            plt.plot(plot_data[i], color=colors[i])
        plt.show()


def do_cal_phase():
    # PhaseValues, plot_data = my_phaser.phase_calibration(
    #     verbose=True
    # )  # Start Phase Calibration
    PhaseValues, plot_data = phase_calibration(
        my_phaser, verbose=True
    )  # Start Phase Calibration
    if ENABLE_PLOTS:
        plt.figure(5)
        plt.title("Phase sweeps of adjacent elements")
        plt.xlabel("Phase difference (degrees)")
        plt.ylabel("Amplitude (ADC counts)")
        for i in range(0, 7):
            plt.plot(PhaseValues, plot_data[i], color=colors[i])
        plt.show()


# Instantiate all the Devices
rpi_ip = None
sdr_ip = None
try:
    hostname, rpi_ip, sdr_ip = resolve_hardware_uris()
    print(f"Hostname: {hostname}")
    print(f"Connecting to rpi: {rpi_ip} and sdr: {sdr_ip}")

    # Pre-flight diagnostics: test hostname resolution
    # Use shared cache to avoid Windows mDNS flakiness on repeated lookups
    print("\n--- Network Diagnostics ---")
    dns_cache = {}
    sdr_ok, sdr_result, sdr_target = _test_hostname_resolution(sdr_ip, dns_cache)
    rpi_ok, rpi_result, rpi_target = _test_hostname_resolution(rpi_ip, dns_cache)

    if not sdr_ok:
        print(f"[FAIL] SDR hostname resolution FAILED for '{sdr_target}': {sdr_result}")
    else:
        print(f"[OK] SDR '{sdr_target}' resolved to: {sdr_result}")

    if not rpi_ok:
        print(f"[FAIL] RPI hostname resolution FAILED for '{rpi_target}': {rpi_result}")
    else:
        print(f"[OK] RPI '{rpi_target}' resolved to: {rpi_result}")

    if not (sdr_ok and rpi_ok):
        print("\nHostname resolution failed. This usually means:")
        if not sdr_ok:
            print("  • SDR (PlutoSDR) is unreachable")
        if not rpi_ok:
            print("  • RPI hostname 'phaser.local' is not reachable")
            print("    (mDNS is not working or hardware is offline)")
        print("\nSolutions:")
        print("  1. Get RPI static IP: Check your router or network admin panel")
        print("  2. Set static IP via environment variables:")
        if sdr_ok:
            print(f"     set PHASER_RPI_URI=ip:192.168.X.X  (replace X.X with RPI IP)")
            print(f"     set PHASER_SDR_URI=ip:{sdr_result}  (SDR is at {sdr_result})")
        else:
            print("     set PHASER_RPI_URI=ip:192.168.X.X")
            print("     set PHASER_SDR_URI=ip:192.168.X.X:50901")
        print("  3. Verify Phaser hardware is powered and on the network")
        print("  4. Check if your network requires special configuration for mDNS")
        sys.exit(1)

    print("\n--- Connecting to Hardware ---")
    my_sdr = adi.ad9361(uri=sdr_ip)
    my_phaser = adi.CN0566(uri=rpi_ip, sdr=my_sdr)
except Exception as e:
    if rpi_ip is None or sdr_ip is None:
        print(f"\n[ERROR] Failed to resolve hardware URIs")
    else:
        print(f"\n[ERROR] Failed to connect to hardware at rpi={rpi_ip} and sdr={sdr_ip}")
    print(f"Details: {type(e).__name__}: {e}")
    print("\nTroubleshooting:")
    print("1. Verify network connectivity:")
    print("   • Phaser hardware must be powered on and connected to network")
    print("   • ping phaser.local (or static IP if mDNS unavailable)")
    print("2. Check RPI is accessible:")
    print("   • ssh <user>@phaser.local")
    print("   • Check IIO daemon: systemctl status iiod")
    print("3. Review PHASER_RPI_URI and PHASER_SDR_URI environment variables")
    print("4. Check config.py for uri_mode and custom URIs")
    print("5. For static IP, set environment variables:")
    print("   export PHASER_RPI_URI='ip:192.168.X.X'")
    print("   export PHASER_SDR_URI='ip:192.168.X.X:50901'")
    sys.exit(1)

my_phaser.sdr = my_sdr  # Set my_phaser.sdr

time.sleep(0.5)


# By default device_mode is "rx"
my_phaser.configure(device_mode="rx")


my_phaser.SDR_init(30000000, config.Tx_freq, config.Rx_freq, 6, -6, 1024)

my_phaser.load_channel_cal()
# First crack at compensating for channel gain mismatch
my_phaser.sdr.rx_hardwaregain_chan0 = (
    my_phaser.sdr.rx_hardwaregain_chan0 + my_phaser.ccal[0]
)
my_phaser.sdr.rx_hardwaregain_chan1 = (
    my_phaser.sdr.rx_hardwaregain_chan1 + my_phaser.ccal[1]
)

# Set up receive frequency. When using HB100, you need to know its frequency
# fairly accurately. Use the cn0566_find_hb100.py script to measure its frequency
# and write out to the cal file. IF using the onboard TX generator, delete
# the cal file and set frequency via config.py.

try:
    my_phaser.SignalFreq = load_hb100_cal()
    print("Found signal freq file, ", my_phaser.SignalFreq)
except:
    my_phaser.SignalFreq = config.SignalFreq
    print("No signal freq found, keeping at ", my_phaser.SignalFreq)
    print("And using TX path. Make sure antenna is connected.")
    config.use_tx = True  # Assume no HB100, use TX path.

# use_tx = config.use_tx
use_tx = False

if use_tx is True:
    # To use tx path, set chan1 gain "high" keep chan0 attenuated.
    my_sdr.tx_hardwaregain_chan0 = int(
        -88
    )  # this is a negative number between 0 and -88
    my_sdr.tx_hardwaregain_chan1 = int(-3)
    my_sdr.tx_lo = config.Tx_freq  # int(2.2e9)

    my_sdr.dds_single_tone(
        int(2e6), 0.9, 1
    )  # sdr.dds_single_tone(tone_freq_hz, tone_scale_0to1, tx_channel)
else:
    # To disable tx, set attenuation to a high value and set frequency far from rx.
    my_sdr.tx_hardwaregain_chan0 = int(
        -88
    )  # this is a negative number between 0 and -88
    my_sdr.tx_hardwaregain_chan1 = int(-88)
    my_sdr.tx_lo = int(1.0e9)


# Configure CN0566 parameters.
#     ADF4159 and ADAR1000 array attributes are exposed directly, although normally
#     accessed through other methods.


# my_phaser.frequency = (10492000000 + 2000000000) // 4 #6247500000//2

# Onboard source w/ external Vivaldi
my_phaser.frequency = (
    int(my_phaser.SignalFreq) + config.Rx_freq
) // 4  # PLL feedback via /4 VCO output
my_phaser.freq_dev_step = 5690
my_phaser.freq_dev_range = 0
my_phaser.freq_dev_time = 0
my_phaser.powerdown = 0
my_phaser.ramp_mode = "disabled"


# This can be useful in Array size vs beam width experiment or beamtappering experiment.
#     Set the gain of outer channels to 0 and beam width will increase and so on.

# To set gain of all channels with different values.
#     Here's where you would apply a window / taper function,
#     but we're starting with rectangular / SINC1.

gain_list = [127, 127, 127, 127, 127, 127, 127, 127]

# Averages decide number of time samples are taken to plot and/or calibrate system. By default it is 1.
my_phaser.Averages = 4

# Aim the beam at boresight by default
my_phaser.set_beam_phase_diff(0.0)

print(
    "Calibrating gain and phase - place antenna at mechanical boresight in front of the array"
)
print("Calibrating gain mismatch between SDR channels, then saving cal file...")
do_cal_channel()
save_channel_cal(getattr(my_phaser, "ccal", [0.0, 0.0]))
print("Calibrating Gain, verbosely, then saving cal file...")
do_cal_gain()  # Start Gain Calibration
save_gain_cal(getattr(my_phaser, "gcal", [1.0] * 8))
print("Calibrating Phase, verbosely, then saving cal file...")
do_cal_phase()  # Start Phase Calibration
save_phase_cal(getattr(my_phaser, "pcal", [0.0] * 8))
print("Done calibration")
if hasattr(my_sdr, "rx_destroy_buffer"):
    my_sdr.rx_destroy_buffer()
del my_sdr
del my_phaser
