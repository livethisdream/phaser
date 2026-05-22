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


# Utility script to find the frequency of an HB100 microwave source.
# Also serves as basic example for setting / stepping the frequency of
# the phaser's PLL, capturing data, calculating FFTs, and stitching together
# FFTs that span several bands.

import time
from time import sleep
import os
import socket
import sys

import numpy as np
import adi
from phaser_functions import save_hb100_cal, spec_est

try:
    import config as config
except ImportError:
    print("Make sure config.py is in this directory")
    sys.exit(1)


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

#  Configure SDR parameters.

try:
    my_sdr._ctrl.debug_attrs["adi,frequency-division-duplex-mode-enable"].value = "1"
    my_sdr._ctrl.debug_attrs[
        "adi,ensm-enable-txnrx-control-enable"
    ].value = "0"  # Disable pin control so spi can move the states
    my_sdr._ctrl.debug_attrs["initialize"].value = "1"
except (AttributeError, KeyError, OSError) as e:
    print(f"[WARNING] Some SDR debug attributes not accessible: {e}")
    print("[INFO] Continuing without those attributes...")

my_sdr.rx_enabled_channels = [0, 1]  # enable Rx1 (voltage0) and Rx2 (voltage1)
my_sdr._rxadc.set_kernel_buffers_count(1)  # No stale buffers to flush
rx = my_sdr._ctrl.find_channel("voltage0")
rx.attrs["quadrature_tracking_en"].value = "1"  # enable quadrature tracking
my_sdr.sample_rate = int(30000000)  # Sampling rate
my_sdr.rx_buffer_size = int(4 * 256)
my_sdr.rx_rf_bandwidth = int(10e6)
# We must be in manual gain control mode (otherwise we won't see the peaks and nulls!)
my_sdr.gain_control_mode_chan0 = "manual"  # DISable AGC
my_sdr.gain_control_mode_chan1 = "manual"
my_sdr.rx_hardwaregain_chan0 = 0  # dB
my_sdr.rx_hardwaregain_chan1 = 0  # dB

my_sdr.rx_lo = int(2.0e9)  # Downconvert by 2GHz  # Receive Freq

my_sdr.filter = "LTE20_MHz.ftr"  # Handy filter for fairly widdeband measurements

# Make sure the Tx channels are attenuated (or off) and their freq is far away from Rx
# this is a negative number between 0 and -88
my_sdr.tx_hardwaregain_chan0 = int(-80)
my_sdr.tx_hardwaregain_chan1 = int(-80)


# Configure CN0566 parameters.
#     ADF4159 and ADAR1000 array attributes are exposed directly, although normally
#     accessed through other methods.


# Set initial PLL frequency to HB100 nominal

my_phaser.SignalFreq = 10.525e9
my_phaser.lo = int(my_phaser.SignalFreq) + my_sdr.rx_lo


gain_list = [64] * 8
for i in range(0, len(gain_list)):
    my_phaser.set_chan_gain(i, gain_list[i], apply_cal=False)

# Aim the beam at boresight (zero degrees). Place HB100 right in front of array.
my_phaser.set_beam_phase_diff(0.0)

# Averages decide number of time samples are taken to plot and/or calibrate system. By default it is 1.
my_phaser.Averages = 8

# Initialize arrays for amplitudes, frequencies
full_ampl = np.empty(0)
full_freqs = np.empty(0)

# Set up range of frequencies to sweep. Sample rate is set to 30Msps,
# for a total of 30MHz of bandwidth (quadrature sampling)
# Filter is 20MHz LTE, so you get a bit less than 20MHz of usable
# bandwidth. Set step size to something less than 20MHz to ensure
# complete coverage.
f_start = 10.0e9
f_stop = 10.7e9
f_step = 10e6

for freq in range(int(f_start), int(f_stop), int(f_step)):
    #    print("frequency: ", freq)
    my_phaser.SignalFreq = freq
    my_phaser.frequency = (
        int(my_phaser.SignalFreq) + my_sdr.rx_lo
    ) // 4  # PLL feedback via /4 VCO output

    data = my_sdr.rx()
    data_sum = data[0] + data[1]
    #    max0 = np.max(abs(data[0]))
    #    max1 = np.max(abs(data[1]))
    #    print("max signals: ", max0, max1)
    ampl, freqs = spec_est(data_sum, 30000000, ref=2**12, plot=False)
    ampl = np.fft.fftshift(ampl)
    ampl = np.flip(ampl)  # Just an experiment...
    freqs = np.fft.fftshift(freqs)
    freqs += freq
    full_freqs = np.concatenate((full_freqs, freqs))
    full_ampl = np.concatenate((full_ampl, ampl))
    sleep(0.1)
full_freqs /= 1e9  # Hz -> GHz

peak_index = np.argmax(full_ampl)
peak_freq = full_freqs[peak_index]
print("Peak frequency found at ", full_freqs[peak_index], " GHz.")

# Non-interactive mode: detect and persist calibration only.

save_hb100_cal(peak_freq * 1e9)
print("HB100 Freq saved to file")
if hasattr(my_sdr, "rx_destroy_buffer"):
    my_sdr.rx_destroy_buffer()
del my_sdr
del my_phaser


