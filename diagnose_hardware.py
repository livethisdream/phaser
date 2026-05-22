#!/usr/bin/env python3
"""Hardware URI diagnostic tool."""

import socket

hostname = socket.gethostname()
fqdn = socket.getfqdn()

print(f"Hostname: {hostname}")
print(f"FQDN: {fqdn}")
print()

if "phaser" in hostname.lower():
    print("[ON-BOARD] Running ON the Phaser hardware board")
    print("  Should use:")
    print("    rpi_uri='ip:localhost'")
    print("    sdr_uri='ip:192.168.2.1'")
else:
    print("[REMOTE] Running on a LAPTOP controlling remote Phaser")
    print("  Should use:")
    print("    rpi_uri='ip:phaser.local' (or specific IP)")
    print("    sdr_uri='ip:phaser.local:50901'")

print()
print("Next steps:")
print("  1. Verify the Phaser hardware is powered on")
print("  2. Test network connectivity:")
print("     ping phaser.local")
print("  3. If mDNS doesn't work, get the hardware IP")
print("     and update the URIs above")

