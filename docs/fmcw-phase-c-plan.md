# FMCW Phase C — hardware path

Status: **draft for review**, 2026-10-24. Nothing here is implemented yet.

Phase B (`phaser_radar_dsp.py`, the FMCW scene in `phaser_sim.py`) gives us the
math. Phase C makes the kit produce data for it: program the ADF4159 ramp,
sync the Pluto capture to it, add an `fmcw_radar` mode, and show just enough
in `frontend-radar/` to check it on the bench. The polished FMCW UI and the JS
port of the simulator are Phase D.

## Sources

- `docs/2025_Phaser_labs_Python.pdf` pp. 29–38 (FMCW range, range waterfall,
  range-Doppler, MTI, CFAR).
- Upstream scripts the labs run, which are the hardware ground truth:
  - `pyadi-iio/examples/phaser/RADAR_FFT_Waterfall.py`: range lab,
    free-running `continuous_triangular` ramp, no chirp sync.
  - `jonkraft/PhaserRadarLabs/Range_Doppler_Plot.py`,
    `FMCW_RADAR_Waterfall_ChirpSync.py`, `CFAR_RADAR_Waterfall_ChirpSync.py`:
    `single_sawtooth_burst` ramp triggered per chirp by the Pluto TDD engine.

## Hardware sequence (from `Range_Doppler_Plot.py`)

1. **ADF4159** (on the Pi, `adi.adf4159(rpi_ip)`; every value is /4 because
   of the CN0566 divider, the same rule `SDR_LO_init` already enforces):
   - `frequency = (output_freq + signal_freq + center_freq) / 4`. This is the
     LO the CW path already uses (`cw_lo_freq`).
   - `freq_dev_range = B/4`, `freq_dev_step = (B/4)/num_steps`,
     `freq_dev_time = ramp_time_us`, `num_steps = ramp_time_us` (1 step/µs).
   - `ramp_mode = "single_sawtooth_burst"`, `tx_trig_en = 1`,
     `delay_word = 4095`, `delay_clk = "PFD"`, every delay enable at 0.
   - `enable = 0` written **last** (that write latches the registers).
   - Read back `freq_dev_time`. The chip rounds it, and the actual value is
     what range math must use.
2. **Pluto TDD** (`adi.tddn(sdr_ip)`, `adi.one_bit_adc_dac(sdr_ip)`):
   - `gpio_tdd_ext_sync = True`, `gpio_phaser_enable = True`.
   - `tdd.enable = False` → `sync_external = True`,
     `frame_length_ms = PRI`, `burst_count = num_chirps`, channels 0–2
     `on_raw=0, off_raw=10` → `tdd.enable = True`.
3. **Pluto SDR**: `sample_rate`, `rx_lo = tx_lo = center_freq`, manual gain,
   `tx_cyclic_buffer = True`, Tx ch0 at -88 dB, ch1 at `tx_gain`, cyclic
   `signal_freq` tone. `rx_buffer_size` = the next power of two ≥
   `PRI × num_chirps × fs` (cap 2^22 in burst mode).
4. **Per frame**: pulse the Phaser's `gpio_burst` 0→1→0 (on our existing
   `self.gpios`), `sdr.rx()`, sum the two channels, then slice
   `num_chirps` windows of `good_ramp_samples`. Each window starts
   `start_offset = on_ms + 10% of ramp` into its PRI slot, skipping the ramp's
   settling start. The result feeds `chirp_matrix()` → `mti_filter()` →
   `range_doppler_map()` / `range_profile()` → `ca_cfar()`.
5. **Exit**: `tdd.enable = False`, `gpio_tdd_ext_sync = False`,
   `ramp_mode = "disabled"`, `tx_destroy_buffer()`, restore the beamforming LO,
   taper and SDR snapshot. This is the same unwind contract as `exit_cw_mode`,
   including tolerating a half-entered mode.

## Work items

### C0. Capability probe (first, small)
The TDD engine needs Pluto firmware ≥ v0.39 and a pyadi-iio with `adi.tddn`.
Neither is guaranteed on a stock Kuiper card. At backend start, probe for
`adi.tddn`, the Pluto firmware version and the ADF4159 ramp attributes, and
report `fmcw: {available, reason}` in state, so the UI can say *why* FMCW is
greyed out instead of failing silently. Unit-tested with fakes.

### C1. `phaser_fmcw_radar.py` (new hardware module, mirrors `phaser_cw_radar.py`)
- Pure functions, no I/O, fully tested:
  `ramp_registers(cfg)`, `tdd_plan(cfg)` (PRI, burst count, buffer size,
  offsets) and `slice_chirps(iq, plan)`.
- I/O functions: `enter_fmcw_mode(sdr, cfg, saved, array, rpi_ip,
  sdr_ip, gpios)`, `capture_fmcw_frame(...)` and `exit_fmcw_mode(...)`.
- Hold one `adi.adf4159` handle for the mode's lifetime instead of reopening
  per write (`SDR_LO_init` opens a fresh context every call, and the todo list
  already flags leaked iio contexts).
- Tests: a recording fake for ADF4159/TDD/SDR that asserts write **order**
  (`enable` last, `tdd.enable` bracketing), the unwind on failure at every step,
  and that exit puts back exactly what enter changed.

### C2. Mode dispatcher
- Add `fmcw_radar` to `_set_mode`, and generalize the CW enter/exit/unwind into
  one per-mode table, so CW → FMCW goes through a full restore and not a
  half-swap.
- Commands: `start_fmcw_radar`, `stop_fmcw_radar`, `set_fmcw_radar_params`,
  `get_fmcw_radar_state`. Processing-only params (MTI, CFAR, window,
  `zero_range_freq`, `range_max`) apply live. Ramp/TDD params need a
  stop/start, as CW already does.
- Calibration runs tear FMCW down first, as they already do for CW.
- `--sim`: route through `SimSDR.set_fmcw_mode`, which already exists, so the
  whole mode path runs in CI without a kit.

### C3. Payload
A 256-chirp × ~1200-sample range-Doppler map is too big to send at frame
rate. Send it cropped to `range_max` and ±v_max, quantized to uint8 dB with
scale and offset in the header, plus the CFAR threshold and detections. A
single-chirp range profile ships as float32. The size budget is enforced by a
test.

### C4. Minimal bench UI in `frontend-radar/`
An FMCW tab with: a range profile (with a frequency/range toggle and a 0 m
calibrate button), a range waterfall, a range-Doppler heatmap, and the ramp,
MTI and CFAR controls. Only enough to run the bench checklist below; layout
and polish are Phase D.

### C5. Bench validation (needs you and the kit)
1. Probe reports FMCW available; an older Pluto firmware reports the reason.
2. Corner reflector at ~0 m: read the 0 m frequency (lab step 21) and store it
   as `zero_range_freq`.
3. At 1, 2 and 3 m: measure Hz per metre and compare with 3.34 kHz/m (see open
   question 3).
4. Steer off target: amplitude drops (lab step 23).
5. Hand wave toward/away: the Doppler sign matches the motion; MTI suppresses
   the static reflector.
6. CFAR: tune bias/guard/ref so the threshold sits between clutter and target.
7. Stop FMCW → beamforming sweep: the HB100 beam is still where it was (LO,
   taper and SDR fully restored).

## Open questions for you

1. **One ramp mode or two?** The range lab runs a free-running
   `continuous_triangular` ramp with no sync. The other three labs use the TDD-synced
   `single_sawtooth_burst`. I recommend synced sawtooth everywhere, which means
   one code path, with free-running triangular only as the fallback when the
   probe finds no TDD.
2. **Firmware on the kits.** Do the workshop Plutos have firmware ≥ v0.39? If not,
   `provision.sh` may need a firmware step. That is its own item, and not in this plan.
3. **The 6.7 kHz/m mystery is probably solved.** `RADAR_FFT_Waterfall.py`
   computes range with `c / (4 × slope)` on its triangular ramp, and
   `Range_Doppler_Plot.py` uses `c / (2 × slope)` on its sawtooth. Lab step
   22's "about 6.7 kHz" matches the first, and Phase B implemented the
   second. Bench step 3 settles which is physically right for each ramp mode.
   Until then I'd make the factor a per-ramp-mode constant rather than guess.
4. **Defaults.** The scripts disagree: `output_freq` is 12.145 GHz (range lab)
   in one and 9.9 GHz (range-Doppler) in the other, and `sample_rate` is 0.6
   vs 4 MHz. Use per-lab presets (my preference, matching how the beamforming
   presets work) or one default set?
5. **Frame-rate target.** The labs warn that the Pi struggles with CFAR. What is
   acceptable: about 5 fps for range-Doppler, faster for a single-chirp range profile?

## Order and sizing

C0 → C1 → C2 → C3 → C4 → C5. Each of C0–C3 lands as its own PR into
`radar-dev`, testable in CI with fakes and `--sim`. C4 can overlap C3. C5 is
the gate before any of it merges to `main`.
