# Hardware Verification Testing

Tests in `tests/test_hardware.py` require a live ADI Phaser device. They are automatically skipped when the hardware is not reachable, so they never break the laptop/CI suite.

## Prerequisites

| Item | Check |
|------|-------|
| Phaser powered on and booted | LED on/green |
| Ethernet connected (Phaser ↔ laptop) | `ping phaser.local` succeeds |
| Python venv synced | `uv sync --extra test` |
| Frontend built (optional) | `npm run build` in `frontend/` |

## Quick start

```powershell
# Default address (phaser.local)
.\scripts\test.ps1 -Markers hardware

# Custom address
.\scripts\test.ps1 -Markers hardware -HardwareUri "ip:192.168.1.10:ip:192.168.1.10:50901"
```

Or directly with uv:

```powershell
uv run python -m pytest tests/test_hardware.py -m hardware -v
```

## Test Plan Overview

### T1 — Connectivity (TestConnectivity)

Verifies each hardware subsystem is reachable before any other test runs.

| Test | What it checks |
|------|----------------|
| `test_gpio_bridge_reachable` | RPi one-bit ADC/DAC GPIO bridge responds at `PHASER_RPI_URI` |
| `test_sdr_reachable` | PlutoSDR connects at `PHASER_SDR_URI` and reports a valid sample rate |
| `test_lo_reachable` | ADF4159 LO frequency is readable via `phaser_server.lo` |
| `test_adar_array_reachable` | ADAR1000 array has both BEAM0 and BEAM1 chips via `phaser_server.array` |

**Expected pass state:** All four pass → hardware stack is correctly wired.  
**If any T1 test fails:** Stop here; fix connectivity before continuing.

> **Windows mDNS note:** pyadi-iio opens a new `libiio` context for each `adi.*` object created with `ip:phaser.local`. After 2–3 simultaneous lookups, Windows mDNS can fail with `[Errno 11001]`. The T1 LO and ADAR tests therefore reuse connections already held open by the `phaser_server` module fixture rather than creating standalone contexts.

---

### T2 — GPIO / Transmit control (TestGPIOControl)

| Test | What it checks |
|------|----------------|
| `test_vctrl_default_state` | `gpio_vctrl_1` and `gpio_vctrl_2` are high (1) after init |
| `test_tx_switch_toggle` | `gpio_tx_sw` can be flipped and read back without error, then restored |
| `test_vctrl2_toggle` | PA-enable line (`gpio_vctrl_2`) can be driven low then high |

**Pass criteria:** All read-back values match the written values within one toggle cycle.

---

### T3 — SDR / PlutoSDR (TestSDR)

| Test | What it checks |
|------|----------------|
| `test_sdr_sample_rate` | `sdr.sample_rate` matches config `SampleRate` within 1 Hz |
| `test_sdr_rx_capture` | `SDR_getData()` returns 2 channels, each `rx_buffer_size` long |
| `test_sdr_rx_not_all_zeros` | At least one channel has non-zero RMS power (noise floor present) |
| `test_sdr_rx_gain_write` | Setting gain to 20 dB reads back ≤ ±1 dB tolerance |
| `test_sdr_lo_frequency` | SDR LO is > 2 GHz (plausibility check) |

**What to look for:**
- If `rx_capture` fails → check dual-channel PlutoSDR firmware.
- If `not_all_zeros` fails → suspect cable or RF chain issue.
- Gain readback ±1 dB is normal for discrete gain steps.

---

### T4 — ADAR / Beamformer (TestADAR)

| Test | What it checks |
|------|----------------|
| `test_adar_set_uniform_taper` | Writing gain=100 to all 8 elements completes without error |
| `test_adar_set_zero_taper` | Writing gain=0 (mute) then restoring to 100 completes without error |
| `test_adar_set_phase_broadside` | PhDelta=0 (broadside) programs all elements cleanly |
| `test_adar_set_phase_steered` | PhDelta=45° steered beam programs cleanly |
| `test_adar_phase_wraps_correctly` | PhDelta=400° wraps without raising (mod-360 logic) |
| `test_adar_element_readback` | After programming, all 8 `rx_phase` values read back in [0, 360) |

**Pass criteria:** No exceptions during write; readback values in valid range.

---

### T5 — LO / ADF4159 (TestLO)

| Test | What it checks |
|------|----------------|
| `test_lo_set_frequency` | Writing 12.725 GHz reads back within ±1 kHz |

**Note:** The test uses the `PhaserServer`'s already-open LO object (`phaser_server.lo`) rather than opening a fresh `adi.adf4159` connection, which avoids a second mDNS lookup that can fail on Windows. The LO is left at the test frequency; it is re-initialised to the correct value when the T6 end-to-end sweep fixture runs.

---

### T6 — End-to-end sweep (TestEndToEndSweep)

These tests exercise the full `PhaserServer.process_sweep()` pipeline against live hardware.

| Test | What it checks |
|------|----------------|
| `test_single_phase_point` | Broadside sweep (PhDelta=0) returns valid arrays |
| `test_response_keys_present` | All 7 WebSocket contract keys present in result |
| `test_gain_values_are_finite` | No NaN or Inf in `ArrayGain` or `max_gain` |
| `test_gain_range_plausible` | Peak `ArrayGain` in [-120, 0] dBFS |
| `test_static_phase_mode` | Static Phase mode with empty PhaseValues returns ≥ 1 point |
| `test_taper_affects_gain` | Zeroing taper produces lower received power than uniform taper |

**The `test_taper_affects_gain` test is the most physically meaningful**: it confirms that the ADAR gain path is actually in the RF chain. The test requires an active HB100 signal — if both measurements are below −55 dBFS (thermal noise floor with transmit disabled), the test automatically **skips** rather than giving a false failure. A skip here means "no signal to measure", not a hardware fault.

---

## Debugging guide

### All T1 tests fail

```powershell
ping phaser.local
# If no response:
#   1. Check Ethernet cable / switch
#   2. Try arp -a to find the IP manually
#   3. Set PHASER_RPI_URI=ip:<ip-address> and PHASER_SDR_URI=ip:<ip-address>:50901
```

### T3 `not_all_zeros` fails (both channels read 0)

- Verify `SDR_init` completes (check stdout for "Connecting to SDR")
- PlutoSDR firmware ≥ 0.35 required for dual-channel
- Try lowering Rx gain: `rx_hardwaregain_chan0` may be clipping to 0

### T4 `element_readback` phase out of range

- ADAR1000 SPI bus issue — check SPI lines between RPi and board
- Verify `ADAR_init` ran (it calls `device.reset()` which primes state)

### T6 `taper_affects_gain` fails (gain_off ≥ gain_on)

Possible causes:
1. **ADAR not in the RF chain** — check if the receive path bypasses the ADAR array
2. **Gain elements saturating at low setting** — noise floor limited; try `Averages=4`
3. **`element_map` mismatch** — if elements don't map to the right devices, gain=0 on wrong elements

### Run a single test for fast iteration

```powershell
uv run python -m pytest tests/test_hardware.py::TestEndToEndSweep::test_taper_affects_gain -v -s
```

---

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `PHASER_RPI_URI` | `ip:phaser.local` | RPi / GPIO / LO / ADAR address |
| `PHASER_SDR_URI` | `ip:phaser.local:50901` | PlutoSDR context address |

Both can be set in `config.py` as `rpi_uri` / `sdr_uri` for persistent overrides.

---

## Sample output (all passing)

```
tests/test_hardware.py::TestConnectivity::test_gpio_bridge_reachable    PASSED
tests/test_hardware.py::TestConnectivity::test_sdr_reachable             PASSED
tests/test_hardware.py::TestConnectivity::test_lo_reachable              PASSED
tests/test_hardware.py::TestConnectivity::test_adar_array_reachable      PASSED
tests/test_hardware.py::TestGPIOControl::test_vctrl_default_state        PASSED
tests/test_hardware.py::TestGPIOControl::test_tx_switch_toggle           PASSED
tests/test_hardware.py::TestGPIOControl::test_vctrl2_toggle              PASSED
tests/test_hardware.py::TestSDR::test_sdr_sample_rate                    PASSED
tests/test_hardware.py::TestSDR::test_sdr_rx_capture                     PASSED
tests/test_hardware.py::TestSDR::test_sdr_rx_not_all_zeros               PASSED
tests/test_hardware.py::TestSDR::test_sdr_rx_gain_write                  PASSED
tests/test_hardware.py::TestSDR::test_sdr_lo_frequency                   PASSED
tests/test_hardware.py::TestADAR::test_adar_set_uniform_taper            PASSED
tests/test_hardware.py::TestADAR::test_adar_set_zero_taper               PASSED
tests/test_hardware.py::TestADAR::test_adar_set_phase_broadside          PASSED
tests/test_hardware.py::TestADAR::test_adar_set_phase_steered            PASSED
tests/test_hardware.py::TestADAR::test_adar_phase_wraps_correctly        PASSED
tests/test_hardware.py::TestADAR::test_adar_element_readback             PASSED
tests/test_hardware.py::TestLO::test_lo_set_frequency                    PASSED
tests/test_hardware.py::TestEndToEndSweep::test_single_phase_point       PASSED
tests/test_hardware.py::TestEndToEndSweep::test_response_keys_present    PASSED
tests/test_hardware.py::TestEndToEndSweep::test_gain_values_are_finite   PASSED
tests/test_hardware.py::TestEndToEndSweep::test_gain_range_plausible     PASSED
tests/test_hardware.py::TestEndToEndSweep::test_static_phase_mode        PASSED
tests/test_hardware.py::TestEndToEndSweep::test_taper_affects_gain       PASSED
25 passed in 48.3s
```

