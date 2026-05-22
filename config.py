# Default configuration for Phaser Server

# Hardware URI selection mode:
# - "auto": keep legacy hostname-based behavior
# - "prefer_config": use configured URIs when present, otherwise fallback to auto
# - "custom": require explicit rpi_uri/sdr_uri values
uri_mode = "prefer_config"

# Edit these values directly for laptop-hosted control of remote hardware.
#rpi_uri = "ip:phaser.local"
rpi_uri = "ip:phaser.local"
sdr_uri = "ip:phaser.local:50901"
#sdr_uri = "ip:169.254.133.224:50901"

SignalFreq = 10.525e9  # HB100 nominal frequency
Tx_freq = 2.2e9        # Pluto Tx LO frequency
Rx_freq = 2.2e9        # Pluto Rx LO frequency
SampleRate = 3e6
Rx_gain = 30
Tx_gain = -10
Averages = 1
d = 0.014              # Antenna element spacing in meters
buffer_size = 1024 * 16

# GUI defaults used by phaser_gui.py
refresh_time = 1000
start_lab = "Enable All"

# Phase calibration values
Rx1_cal = 0.0
Rx2_cal = 0.0
Rx3_cal = 0.0
Rx4_cal = 0.0
Rx5_cal = 0.0
Rx6_cal = 0.0
Rx7_cal = 0.0
Rx8_cal = 0.0
