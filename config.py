# Default configuration for Phaser Server
# Kept aligned with release/PhaserBundle/config.py — the Pi's copy is what
# actually runs in production; this file exists so the app can be imported
# and run locally in --sim mode.

# Hardware URI selection mode:
# - "auto": keep legacy hostname-based behavior
# - "prefer_config": use configured URIs when present, otherwise fallback to auto
# - "custom": require explicit rpi_uri/sdr_uri values
uri_mode = "prefer_config"

# Override these in config_custom.py for laptop-hosted control of remote hardware.
rpi_uri = "ip:phaser.local"
sdr_uri = "ip:phaser.local:50901"

SignalFreq = 10.525e9  # HB100 nominal frequency
Tx_freq = 2.2e9        # Pluto Tx LO frequency
Rx_freq = 2.2e9        # Pluto Rx LO frequency
SampleRate = 3e6
Rx_gain = 30
Tx_gain = -10
Averages = 1
d = 0.014              # Antenna element spacing in meters
buffer_size = 1024 * 16

# Phase calibration values
Rx1_cal = 0.0
Rx2_cal = 0.0
Rx3_cal = 0.0
Rx4_cal = 0.0
Rx5_cal = 0.0
Rx6_cal = 0.0
Rx7_cal = 0.0
Rx8_cal = 0.0
