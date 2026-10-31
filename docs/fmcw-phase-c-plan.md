# FMCW Phase C — hardware path

Status, 2026-10-26:
- **C0 is done.** The probe passes on phaser-01 (Pluto v0.39), reporting
  `single_sawtooth_burst`.
- **The open questions are decided** (see *Decisions* at the end).
- **C1 is done.** Merged to `radar-dev` as PR #57.
- **C2 is planned in detail** below, and is not implemented yet.

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
   - `gpio_phaser_enable = True`. Also `gpio_tdd_ext_sync = True`, but
     **only if the channel exists**: Pluto v0.39 has none (only
     `phaser_enable` and `muxout`), and chirp sync works without it. Bench
     result 2026-10-26: `Range_Doppler_Plot.py` gave a live range-Doppler map
     on a v0.39 kit, where the script's `tdd_ext_sync` write is a silent no-op.
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
5. **Exit**: `tdd.enable = False`, `gpio_tdd_ext_sync = False` (if present),
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

Detailed 2026-10-26, after reading `Range_Doppler_Plot.py`,
`FMCW_RADAR_Waterfall_ChirpSync.py`, `CFAR_RADAR_Waterfall_ChirpSync.py` and
`RADAR_FFT_Waterfall.py`. C1 adds no mode, no command and no UI. It is a module
and its tests, and nothing calls it until C2.

**Pure part: config, presets and plans.** Goes in `phaser_radar_dsp.py`, which
imports no hardware, so the tests run without pyadi-iio.

- `FMCW_PRESETS`, one per lab, taken from the script each lab runs:

  | preset | script | fs | output | ramp | chirps | PRI guard |
  |---|---|---|---|---|---|---|
  | `range` | `RADAR_FFT_Waterfall.py` | 0.6 MHz | 12.145 GHz | 1.2 ms | 1 | n/a (free-running) |
  | `range_sync` | `FMCW_RADAR_Waterfall_ChirpSync.py` | 5 MHz | 10 GHz | 500 µs | 1 | +1.0 ms |
  | `cfar` | `CFAR_RADAR_Waterfall_ChirpSync.py` | 2 MHz | 10 GHz | 500 µs | 1 | +1.0 ms |
  | `range_doppler` | `Range_Doppler_Plot.py` | 4 MHz | 9.9 GHz | 300 µs | 256 | +0.2 ms |

  All use B = 500 MHz and a 100 kHz IF tone. The values in each script get
  checked against lab pp. 29–38 while writing the presets; where they
  disagree, the lab wins and the test says why. **`center_freq` is not in
  any preset.** It defaults to the kit's characterized IF (`self.Rx_freq`,
  from `calibration.json`). The scripts' 2.1 GHz and our `FMCW_DEFAULTS`
  2.2 GHz are both above phaser-00's 2.18 GHz ceiling or close to it (see
  `docs/calibration-bugs.md`). C1 takes `center_freq` from the cfg, so the
  wiring belongs to C2.
- `ramp_registers(cfg, ramp_mode)` returns an **ordered list** of
  `(attr, value)` pairs for an `adi.adf4159`, in upstream's order:
  - `frequency = cw_lo_freq(cfg) / 4`
  - `freq_dev_range = B/4`, `freq_dev_step = (B/4)/num_steps`,
    `freq_dev_time = ramp_us`
  - the delay and enable bits: `delay_word = 4095`, `delay_clk = "PFD"`,
    the three delay enables at 0
  - `ramp_mode`, `sing_ful_tri = 0`, and `tx_trig_en` = 1 for the synced
    ramp, 0 for the free-running one
  - `enable = 0` **last**

  Writing it in a loop makes "`enable` last" a property of the data, which
  is easy to test. All values are ints, and every `/4` goes through one
  helper.
- `chirp_plan(cfg, ramp_mode, actual_ramp_us, on_ms=0.0)` replaces
  `tdd_plan` from the first draft, because the free-running ramp needs a
  plan too. It takes the ramp time the ADF4159 **read back**, never the
  requested one, and returns:
  - `pri_ms` = ramp + `pri_guard_ms`, `burst_count`, and
    `frame_samples` = `PRI × fs`
  - `begin_offset_samples` = 10% of the ramp, `good_ramp_samples` = the
    other 90%, and `start_offset_samples` = `on_ms` + the 10%
  - `rx_buffer_size`, the next power of two ≥ `PRI × chirps × fs`, at
    least 2^13
  - `effective_ramp_time` (next bullet)

  It raises `ValueError` when the burst needs more than 2^22 samples.
  Upstream silently caps the buffer and slices past the end of it. For the
  synced ramp, `pri_ms` and `burst_count` are the values `tdd` is
  programmed with, so the TDD and the slicer can't disagree. For the
  free-running ramp, the buffer is the preset's `fft_size` and nothing is
  sliced.
- **Range factor (Q3).** `effective_ramp_time` is the ramp time to hand
  `phaser_radar_dsp`'s existing `c/(2·S)` functions:
  - sawtooth: the read-back time
  - triangle: half of it, which reproduces `RADAR_FFT_Waterfall.py`'s
    `c/(4·S)`

  It comes from one table, `RANGE_TIME_SCALE = {sawtooth: 1.0, triangle:
  0.5}`. So the Phase B DSP is unchanged, and C5 bench step 3 corrects
  one number if the triangle turns out to be wrong. My reading is that
  the triangle sweeps B in each half of `freq_dev_time`. That would make
  0.5 physically right and the lab's "6.7 kHz/m" correct for that ramp,
  but this is unverified until the bench.
- `slice_chirps(iq, plan)` takes the window at `start_offset + m ×
  frame_samples` for `good_ramp_samples` samples, for each chirp `m`. It
  returns the samples × chirps matrix that `chirp_matrix()` already
  produces, so `mti_filter()` / `range_doppler_map()` take it unchanged.
  It raises if the last window runs past the buffer. For a free-running
  plan, it returns the buffer unchanged.

**Hardware part: `phaser_fmcw_radar.py`.**

- `FmcwHardware`, a small holder that opens the three new handles once per
  mode entry:
  - `adi.adf4159(rpi_ip)`
  - `adi.tddn(sdr_ip)`
  - `adi.one_bit_adc_dac(sdr_ip)` for the Pluto GPIOs

  It takes the backend's existing Phaser `gpios` (for `burst`) instead of
  opening another one. It drops all three on exit, and the LO restore goes
  through the held ADF4159 handle, not `SDR_LO_init`, so the mode leaks no
  contexts. `adi` is passed in, so the tests inject a fake module.
- `enter_fmcw_mode(sdr, cfg, saved, hw, array, ramp_mode)` follows
  `Range_Doppler_Plot.py`'s order, which is the one proven on the bench:
  1. SDR snapshot (see *Shared with CW* below)
  2. taper
  3. SDR rates, LOs, gains, `rx_buffer_size`, `tx_cyclic_buffer = True`
  4. ADF4159 registers, then read back `freq_dev_time` and build the plan
     from it
  5. TDD, synced only: `phaser_enable = True`; `tdd_ext_sync = True` only
     if the channel exists; `tdd.enable = False` → `sync_external`,
     `startup_delay_ms = 0`, `frame_length_ms`, `burst_count`, channels
     0–2 → `tdd.enable = True`
  6. cyclic Tx tone, ch0 at −88 dB
  7. discard one frame, because of the stale-buffer lesson from
     calibration bug 3

  Before each step it records a flag in `saved`, so the unwind knows how
  far it got. It returns `(cfg, plan)`.
- `capture_fmcw_frame(sdr, gpios, plan)`:
  - synced: pulse `gpio_burst` 0→1→0, then `rx()`
  - free-running: `rx()` only

  Then it sums ch0 + ch1 and returns `slice_chirps(...)`.
- `exit_fmcw_mode(sdr, saved, hw, array, restore_taper, restore_lo_freq)`
  undoes only what the `saved` flags say landed. Each step is in its own
  `try`, so it tolerates being half-entered and being called twice:
  1. `tdd.enable = False`
  2. `phaser_enable` and `tdd_ext_sync` back to their snapshotted values
  3. on the held handle: `ramp_mode = "disabled"`, `tx_trig_en = 0`,
     `frequency = LO/4`, then `enable = 0` last
  4. `tx_destroy_buffer()`
  5. SDR restore, including `tx_cyclic_buffer` and `rx_buffer_size`
  6. taper

  The TDD has to be off before the beamforming sweep resumes: with
  `sync_external` still set, every plain `rx()` waits for a trigger that
  never comes.

**Shared with CW.** The SDR snapshot and restore code is inline in
`enter_cw_mode`/`exit_cw_mode`. C1 moves it into `snapshot_sdr()` /
`restore_sdr()` in `phaser_cw_radar.py` without changing its behaviour,
adds `tx_cyclic_buffer` to it, and both modes call it. The CW tests guard
the move.

**Tests** (`tests/test_fmcw_hw.py`, plus parity cases in
`tests/test_fmcw_dsp.py`):
- **Golden plan.** The `range_doppler` preset must reproduce
  `Range_Doppler_Plot.py` exactly: `frame_samples` 2000,
  `good_ramp_samples` 1080, `start_offset` 120, buffer 2^19 for 128 ms.
  The same check runs for each other preset against its script.
- **Registers.** The `/4` rule, `step × num_steps = range`, `enable` last,
  and `tx_trig_en` per ramp mode.
- **Plan errors.** A burst over 2^22 raises, and a PRI shorter than the
  ramp raises.
- **Slicing.** A synthetic buffer with each chirp's window marked comes
  back in order. A short buffer raises. Slicing a synced `SimSDR`-style
  frame gives the same range peak as Phase B's `chirp_matrix` on the same
  target.
- **Write order**, on a recording fake of `adi`, the SDR and the GPIOs:
  `enable` is the last ADF4159 write, `tdd.enable` brackets the TDD
  config, `tdd_ext_sync` is never written when the channel is absent, and
  the read-back `freq_dev_time` is what the plan uses.
- **Unwind.** Parametrized over a failure injected at each enter step: the
  exit leaves every recorded attribute at its pre-enter value, the TDD
  off, the ramp disabled, and no handle open. Enter → exit leaves the
  fakes exactly as they started. Exiting twice is harmless.

**Not in C1, and recorded for C2:**
- **`SimSDR` layout.** `_synthesize_fmcw` lays chirps back to back with no
  PRI gap or settling offset. C2 makes it lay them out the way a plan says,
  so `--sim` runs `slice_chirps` too and doesn't skip it.
- **CW `center_freq`.** CW mode has the same 2.2 GHz problem today:
  `DEFAULTS["center_freq"]` ignores the per-unit IF. That's a CW bug,
  separate from this work.

### C2. Mode dispatcher

Detailed 2026-10-26, after C1 merged (PR #57). C2 makes FMCW a mode the
backend can enter, run and leave, on the kit and under `--sim`. It changes
`phaser_headless.py` and `phaser_sim.py` only. The browser simulator keeps
answering FMCW as unavailable: porting FMCW to JS is Phase D, and the parity
test has no FMCW cases to break.

**1. One table for mode transitions.** `_set_mode` today has CW wired in as
an if-chain. It becomes a table, `{mode: (enter, exit)}`, holding `cw_radar`
and `fmcw_radar` (`idle` and `sweep` need neither).
- A transition always runs the old mode's exit before the new mode's enter.
  So CW → FMCW goes through a full restore, never a half-swap.
- A failed enter runs that mode's own exit as the unwind, then lands in
  `idle` and returns the error. This is CW's existing contract, made
  general.
- The CW code moves into `_enter_cw` / `_exit_cw` and otherwise stays the
  same. The CW tests guard the move.
- `stop()` and calibration already drop to `idle` through `_set_mode`
  (calibration for any mode; `stop()` only for CW today). Both cover FMCW
  once the table does.

**2. Frames hold the mode lock.** The main loop captures CW frames without
`cw_lock`, so a stop arriving from the WebSocket thread can tear the SDR
down underneath a capture in progress. FMCW makes that worse: a
range-Doppler capture is a triggered 128 ms burst, and exit turns the TDD
off. So each radar frame's capture runs under the lock:
- `cw_lock` is renamed `mode_lock`, keeping `cw_lock` as an alias.
- A transition then waits at most one frame. This fixes the same race for
  CW.

**3. Entering `fmcw_radar`** (`_enter_fmcw`):
- **Refuse when unavailable.** If the startup probe says FMCW is
  unavailable, refuse with the probe's `reason`.
- **Ramp mode** comes from the probe: synced sawtooth, or the free-running
  triangle.
- **Preset fit.** A preset with `num_chirps > 1` on a kit with no TDD is
  refused, with a message saying range-Doppler needs chirp sync.
- **Config** = `fmcw_config(self.fmcw_params, preset)`.
  - `preset` comes from the client's params and defaults to `range`, the
    first FMCW lab.
  - `center_freq` defaults to `self.Rx_freq`, the kit's characterized IF.
    A value the client sends wins.
- **Hardware.**
  - On a kit: `FmcwHardware.open(adi, self.rpi_ip, self.sdr_ip,
    self.gpios, synced)`.
  - Under `--sim`: new stubs from `phaser_sim`: an ADF4159 that reads
    back `freq_dev_time`, a TDD with three channels, and Pluto pins
    labelled `phaser_enable` only, like v0.39. So `--sim` runs the same
    `enter_fmcw_mode` / `exit_fmcw_mode` code a kit runs, not a shortcut
    around it.
- **Then** `enter_fmcw_mode(...)` stores `(cfg, plan)` and hands the plan
  to the simulator.
- **Exit**: `exit_fmcw_mode` with the beamforming taper
  (`_apply_gain_cal(gainList)`, as CW does) and `restore_lo_freq =
  self.LO_freq`. Then the simulator is switched back to the beamforming
  scene.

**4. `SimSDR` follows the plan.** `set_fmcw_mode(True, plan=plan, ...)`
makes `_synthesize_fmcw` put chirp `m` at `m × frame_samples`. The beat
tone is present only for the ramp's length, with noise for the rest of the
PRI. That makes `--sim` exercise `slice_chirps` and its offsets. Called
with no plan, it keeps today's back-to-back layout, so `test_fmcw_sim.py`
and any other existing callers are unchanged.

**5. Frames** (`do_fmcw_radar_frame`, in the main loop as
`fmcw_radar_frame`):
- Capture with `capture_fmcw_frame`.
- **One chirp, or the free-running ramp:** `range_profile` on the plan's
  `effective_ramp_time`. `zero_range_freq` falls back to `signal_freq`.
- **Several chirps:** `mti_filter` then `range_doppler_map`, with
  `pri = pri_ms / 1e3`.
- **CFAR**, when enabled, runs on the single-chirp range profile only, as
  the CFAR lab does. Whether and how it runs on a range-Doppler map is a
  C3 decision.
- **The C2 payload is scalars:** peak range, peak velocity, peak dB,
  detection count, the plan summary, and `capture_ms` / `process_ms`. The
  arrays and their encoding are C3. Scalars are enough to check the mode
  on a kit with a script, and the timings measure the 5 fps budget.

**6. Commands.**
- **`start_fmcw_radar(params)`** merges `params` (which may include
  `preset`) and enters.
- **`stop_fmcw_radar`** goes to `idle`.
- **`set_fmcw_radar_params(params)`**:
  - **Live on the next frame:** `mti`, `fft_window`, `range_max`,
    `vel_max`, `zero_range_freq`, and the CFAR keys.
  - **Live as writes:** `taper` (array write); `rx_gain` / `tx_gain` (SDR
    writes, as CW does).
  - **Anything else** (preset, ramp, timing, frequencies, `num_chirps`)
    changes the plan. While running, the backend re-enters the mode
    itself: exit then enter, under the lock. The response says so. CW
    makes the client stop and start; FMCW has too many such knobs for
    that to be pleasant.
- **`get_fmcw_radar_state`** returns the mode, whether it's running, the
  effective cfg, the current plan, the probe result, and the preset names.
- **Defaults:** CFAR keys (`cfar`, `cfar_guard`, `cfar_ref`,
  `cfar_bias_db`) join the FMCW defaults in `phaser_radar_dsp`.

**7. Browser simulator.** `start_fmcw_radar` gets the same explicit
"not available in the browser simulator" error that `start_cw_radar`
already gets in `frontend/src/sim/worker.js`. Today it falls through to
the unknown-command path.

**Tests** (`tests/test_fmcw_radar_headless.py`, built like the CW headless
tests on a sim-mode backend with no sockets):
- **Ranging:** starting with the `range_sync` and `range_doppler` presets
  gives frames whose peak range matches the sim's default target, through
  the real slicing.
- **Restore:**
  - Stop puts back the SDR settings, the taper, and the LO written through
    the stub ADF4159.
  - A beamforming sweep runs normally afterwards.
- **Transitions:** CW → FMCW → CW → idle restores fully at every step.
- **Refusals:**
  - An unavailable probe refuses with its reason and stays `idle`.
  - A free-running probe refuses `range_doppler` but runs `range`.
- **Failed enter:** a failure injected in the stub hardware during enter
  leaves the backend `idle` and the hardware restored.
- **`center_freq`:** defaults to `Rx_freq`; an explicit one wins.
- **Params:**
  - A processing param applies with no re-enter.
  - A ramp param re-enters, and the plan changes.
- **Calibration and shutdown:** starting a calibration from `fmcw_radar`
  tears it down first (subprocess mocked), and so does `stop()`.
- **Frame lock:** a transition requested during a frame waits for it.
  This is checked with a capture that blocks on an event.
- **`test_fmcw_sim.py`:** plan-driven layout puts chirps in their PRI
  slots and slicing recovers the range; no-plan layout is unchanged.

**Not in C2:** arrays in the payload and their size budget (C3); any UI
(C4); a kit run (C5, though once C2 lands, a script over the WebSocket can
try FMCW on phaser-01).

### C3. Payload
A 256-chirp × ~1200-sample range-Doppler map is too big to send at frame
rate. Send it cropped to `range_max` and ±v_max, quantized to uint8 dB with
scale and offset in the header, plus the CFAR threshold and detections. A
single-chirp range profile ships as float32. The size budget is enforced by a
test.

**Done (2026-10-30).** `fmcw_radar_frame` messages carry, in `data`:

- every frame: `kind`, `preset`, `ramp_mode`, `num_chirps`, `pri_ms`,
  `ramp_time`, `peak_range`, `peak_magnitude_db`, `range_resolution`,
  `capture_ms`, `process_ms`.
- `kind: "range_doppler"`: `peak_velocity`, `velocity_resolution`,
  `max_velocity`, `range_axis` and `velocity_axis` (m, m/s, 4 decimals), and
  `map` = `{encoding: "u8-base64", rows, cols, db_min, db_max, data}`:
  rows are range and columns velocity, row-major, one byte a cell, and
  `dB = db_min + byte * (db_max - db_min) / 255`. Quantized from the frame's
  peak down to 100 dB below it (`MAP_SPAN_DB`); lower is clipped to the floor.
- `kind: "range_profile"`: `max_range`, `range_axis`, `profile_db` (2
  decimals), and with `cfar` on, `cfar_threshold_db` (same length),
  `detections` (count) and `detection_indices` (into the profile).

`encode_db_u8` / `decode_db_u8` in `phaser_radar_dsp` are the reference.
Measured under `--sim`: `range_doppler` 23.6 KB a frame (61 x 256), `_far`
106 KB (301 x 256), profiles under 2 KB; JSON serialization under 0.4 ms.
Budgets (32 / 128 / 8 KB) are enforced in `tests/test_fmcw_radar_headless.py`,
and `broadcast_to_ws` now serializes once per frame rather than per client.

### C4. Minimal bench UI in `frontend-radar/`
An FMCW tab with: a range profile (with a frequency/range toggle and a 0 m
calibrate button), a range waterfall, a range-Doppler heatmap, and the ramp,
MTI and CFAR controls. Only enough to run the bench checklist below; layout
and polish are Phase D.

**Done (2026-10-30), checked in headless Chromium against `--sim`.** A CW /
FMCW switch in the header swaps the tabs, the Start button and the open
settings section. FMCW tabs: **Range** (the profile, or the strongest cell
per range bin for range-Doppler, with the CFAR threshold and detection
markers), **Range Waterfall**, and **Range-Doppler** (the decoded map). The
FMCW section has the preset, Range Max (empty = the preset's), MTI, Dynamic
Range (the maps' colour scale runs from each frame's peak down this far --
FMCW levels sit tens of dB above CW's, so the CW floor/ceiling saturated
them), CFAR with guard/ref/bias, and the 0 m calibration: a 0 m Beat field
plus "Set 0 m Here", which turns the current peak into 0 m (lab step 21),
and Reset. The capability check shows there, and FMCW Start is disabled with
the reason on a kit that cannot run it.

Found while testing it, and fixed:
- `range_sync` ran at ~475 fps in --sim (~5 ms refills on a kit too), and
  the page fell minutes behind its own clicks redrawing every frame. Radar
  frames are now capped at `RADAR_MAX_FPS` = 20 in the backend, and the page
  draws at most once per display refresh, newest frame first.
- A start on an already running FMCW mode stored the new params and kept the
  old config; it now applies them as `set_fmcw_radar_params` does.
- A page loaded while a mode ran offered Start; it now syncs from
  `get_fmcw_radar_state` / `get_cw_radar_state` on connect.
- `center_freq: null` and `range_max: null` (empty fields) now mean the kit's
  IF and the preset's range, not a crash and an uncropped profile.

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

## Open questions (as asked 2026-10-24; answered under *Decisions*)

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

## Decisions (2026-10-26)

1. **Ramp mode.** The synced `single_sawtooth_burst` is the one code path.
   The free-running `continuous_triangular` is used only when the probe finds
   no TDD sync.
2. **Firmware.** v0.39 is enough. Two kits confirm it: on the earlier kit,
   `Range_Doppler_Plot.py` drew a live map, and phaser-01's probe passes. No
   firmware step goes in `provision.sh`.
3. **Range factor.** Each ramp mode gets its own constant
   (`RANGE_TIME_SCALE`): 1.0 for the sawtooth, 0.5 for the triangle. C5 bench
   step 3 decides whether those are right.
4. **Defaults.** One preset per lab (`FMCW_PRESETS`), with each preset's
   values taken from the script that lab runs. `center_freq` comes from the
   kit's characterized IF, never from a preset.
5. **Frame rate.** The target is about 5 fps for range-Doppler. A
   `range_doppler` frame is 128 ms of capture alone, which caps it near
   7.8 fps before any DSP, so C3 has to keep processing and transport under
   about 70 ms. A single-chirp range profile should run faster. C3 measures
   both on the Pi.

## Bench findings (2026-10-27, phaser-01)

Three problems found timing captures against `Range_Doppler_Plot.py`, the
first three fixed in `fix/fmcw-coherence`.

- **Chirps were not coherent.** `build_iq_tone` snapped the 100 kHz tone to a
  bin of 2**18 samples (100006.1 Hz), so it advanced +1.9 deg a chirp. Every
  stationary return sat ~2 Doppler bins up (+0.24 m/s), and through the MTI.
  Leakage at zero Doppler: 55% ours, 98.7% upstream. The Tx buffer is now
  sized to whole tone periods (`coherent_tone_length`); the plan reports
  `chirp_phase_step_deg`, and enter warns when it is not 0. After: 98.7%,
  +0.01 deg. 2-pulse MTI still only cancels ~21 dB, upstream too: that is the
  hardware's phase noise.
- **`rx_buffer_size` never took effect after the first capture.** pyadi reads
  it only when it creates the buffer. `set_rx_buffer_size` destroys first; CW,
  FMCW and the restore on exit all use it.
- **Short synced captures never completed.** The first `rx()` created the Rx
  buffer after the burst had fired. 256 chirps (128 ms) outlasted the setup; 1
  chirp did not, so `range_sync` and `cfar` timed out from cold (and only ran
  in the service on the stale `range_doppler` buffer). Synced enter now arms
  the buffer first (`arm_rx`), as the ChirpSync scripts do. After: 1-chirp
  `range_sync` refills in 4.5 ms, `cfar` in 7 ms.
- **Capture time is the transfer, not our code** (not fixed). `range_doppler`
  at 2**19 samples: refill ~307 ms, four per-channel reads ~119 ms, ~554 ms
  frame to frame, identical to upstream. 5 fps needs a smaller capture; one
  raw read plus a numpy de-interleave may save ~100 ms (unmeasured).

## Next (as of 2026-10-28)

PR #60 (FMCW coherence, Rx buffer resize and arming, TDD release with
channel 2 high; calibration-bugs.md bug 6) and PR #61 (CW `center_freq`
defaults to the kit IF) are merged and verified on phaser-01. Still open,
in order:

1. **CW peak readout sits on the leakage line.** `process_cw_frame` takes the
   argmax over the whole window, which includes the 0 m/s Tx→Rx leakage, so
   the reported velocity is always about 0 m/s and jitters. Exclude about ±0.3 m/s
   from the peak search. Python first, then `frontend/src/sim/`, plus a case in
   `tests/test_sim_parity.py`.
2. **Shutdown traceback.** Every service stop logs `Exception in thread
   Thread-1` from the exiting process. Harmless so far, but it hides real
   faults in the journal.
3. **Capture speed (PR B).** Measured on phaser-00, 2026-10-29, with
   `tools/bench_fmcw_capture.py` (`range_doppler`, 2**19 samples, 20 frames,
   medians): 693 ms end to end = 572 capture + 121 DSP, i.e. 1.4 fps. The
   capture is a 311 ms refill (the USB transfer, which no read strategy
   shortens) plus 244 ms of pyadi's per-channel read. One `buffer.read()` and a
   numpy de-interleave took 39 ms and was bit-identical on 20/20 frames, so the
   synced capture now uses it (`raw_read_ok`, `read_summed`): about 488 ms, or
   ~2 fps (confirmed: 479 ms). **5 fps still needs the transfer cut**: fewer
   samples per frame (chirps, sample rate, or buffer rounding); that is C3.
   The DSP part is done: the same tool's breakdown (numpy 1.24, scipy
   present, 4 cores) put 121 ms in `range_doppler_map`, 48 of it a fast-time
   FFT of 1079 = 13 * 83 samples. It now crops to `range_max` before the
   slow-time FFT, pads the fast-time FFT to a 5-smooth length, and uses
   `scipy.fft` in complex64 on all cores when scipy is installed (31 ms
   measured for crop + scipy before the padding; within 0.0012 dB).
   For the transfer, `range_doppler` now runs at 2 MS/s instead of the
   script's 4 (2026-10-29 decision): half the buffer, ~90 m to Nyquist
   against a 20 m display, same velocity resolution. `range_doppler_far` is
   the script exactly (4 MS/s, ~180 m, display to 100 m) for outdoor targets,
   and any value can still be overridden per run. Measured on phaser-01:
   262 ms (3.8 fps) for `range_doppler`, 414 ms for `_far`. 5 fps was out of
   reach as configured: the refill is the burst itself in real time (256
   chirps x 0.5 ms = 131 ms) plus the transfer (~12 MB/s, 89 ms at 2 MS/s).
   **Prefetch** (`capture_fmcw_frame`, `prefetch` param, on by default)
   fires the next burst as soon as a frame's samples are off the Pluto, so
   those 131 ms overlap the read, DSP and broadcast; expected ~220 ms
   (~4.5 fps), to measure with `bench_fmcw_capture.py` against
   `--no-prefetch`. Beyond that only fewer chirps help (128: ~135 ms, at
   0.24 m/s velocity resolution) -- a possible `range_doppler_fast` preset.
4. **C3, C4, C5** above.
5. **Pi clock.** No RTC and no NTP client; `phaser-clock` tries HTTP once at
   boot, so a late network leaves the clock wrong (tar warns about future
   timestamps on install).
6. **Merge `radar-dev` into `main`** once the radar work is ready.

## Follow-on: Phase D, browser radar simulator (decided 2026-10-31)

After C5 passes on the kit and #75/#76 are merged, so the port copies
behavior the hardware has confirmed. The Python `--sim` already synthesizes
CW and FMCW (a corner reflector at 1 m, a walker at 3.5 m / 1.8 m/s); what is
missing is a browser-only version, so the radar UI runs with no Pi and no
Python -- a public demo on GitHub Pages, and lab practice before a student
has a kit.

Scope, to be planned in phases and reviewed before any code:

1. Port the radar half of `phaser_sim.py` (`_synthesize_cw`,
   `_synthesize_fmcw`, the targets) to `frontend/src/sim/` or a radar
   sibling, Python first as the source of truth (CLAUDE.md, *Two simulators*).
2. Port the processing in `phaser_radar_dsp.py` that the UI depends on:
   `process_cw_frame`, `range_profile`, `range_doppler_map` (with
   `peak_exclude` and `cal_peak_range`), `mti_filter`, `ca_cfar` /
   `apply_cfar`, and the u8 map encoding or a direct path around it.
3. A sim transport for `frontend-radar/`, mirroring `transport-web.js` the way
   `frontend/src/sim/worker.js` mirrors the WebSocket protocol, run in a
   worker so a 256-chirp frame does not block the page.
4. Extend `tests/test_sim_parity.py` with radar cases (each preset, MTI on and
   off, CFAR on) -- parity is only as strong as that matrix.
5. Publish the radar UI on Pages beside the beamforming demo, with the same
   three guards that keep the sim default off hardware builds.
