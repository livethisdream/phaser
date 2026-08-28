# Beam pattern handoff

Status of the "HB100 shows on the FFT but there is no beam pattern"
investigation, written so a fresh session can pick it up without the original
conversation. Branch: `claude/hb100-beam-pattern-display-3ud5v3`.

## Symptom

The FFT plot shows a healthy HB100 peak. The beam-pattern plot is a flat line.
Reported as "it worked back in July".

## What was wrong

Four defects, all in `phaser_headless.py` and the helpers it calls. Each was
found by diffing against the known-good legacy: ADI's `phaser_gui.py`,
`examples/phaser/ADAR_pyadi_functions.py`, and `examples/phaser/SDR_functions.py`
in pyadi-iio. Any one of the first two is enough to flatten the pattern on its
own.

1. **Nothing latched the ADAR1000 beam state.** `element.rx_phase` and
   `rx_gain` write SPI shadow registers; only `array.latch_rx_settings()`
   moves them into the beam state the RF path uses. There was not one latch
   call in the repo. The sweep wrote 162 steering phases per frame and the
   array answered from whatever was last latched, all 162 times.

2. **`ADAR_init` was `device.reset()` and nothing else.** Reset leaves the Rx
   LNA, VGA and vector modulator powered down and the beam state driven from
   on-chip RAM rather than SPI. Signal still passes -- hence the FFT peak --
   but the vector modulator is the phase shifter, so the array cannot steer.

3. **`phase_cal` and `channel_cal` were loaded, printed, and never applied.**
   pcal is what makes the eight elements add coherently. `phaser_service.py`
   applies both correctly; `phaser_headless.py`, the one that actually runs,
   applied neither. `gain_cal` was applied on the `set_taper` path but not to
   the initial taper, and at the wrong scale (frontend sends 0-100, the
   register is 0-127; legacy bridges with `* 127 / 100`).

4. **`ADAR_set_Phase` quantized the whole per-element phase** -- steering ramp
   plus offsets -- to `phase_step_size`. Legacy quantizes only the ramp, so a
   3-bit lab setting no longer rounds the calibration off to the nearest 45
   degrees. Separately, `do_sweep` used `phase_step` as the sweep's angular
   resolution as well, so dropping to 3 bits collapsed the scan to five points.

A fifth, unrelated to the code: the Pi's `config.py` carries `Rx_gain = 1`
against a repo default of 30. `config.py` is site-owned and never deployed
over. At gain 1 the measured peak prominence is 18.4 dB versus 45.3 dB at
gain 30. Because each sweep point is a time-domain peak sample (legacy does
this too, deliberately), null depth is set by the noise floor -- so a starved
Rx gain flattens the pattern even when everything else is right.

## What is verified, and what is not

Verified in `--sim` (`tests/test_beam_pattern.py`, 13 tests):

    unlatched (before)   0.25 dB across the whole scan, peak at -90 deg
    latched (fixed)     26.7 dB peak-to-null, main lobe at 0.0 deg

and, against a simulated array given per-element phase errors:

    no pcal    peak -13.04 dBFS at -0.91 deg, sidelobes  6.3 dB down
    with pcal  peak -12.19 dBFS at  0.00 deg, sidelobes 11.6 dB down

Note the second table: phase errors barely move the peak. They fill the nulls
and lift the sidelobes. Judge the pattern by sidelobe level, not peak position.

The simulator now models the latch (elements carry a shadow and a latched beam
state) and an intrinsic per-element phase error, which is what makes those
tests sensitive to these bugs at all.

**Verified on hardware**, 2026-08-27, on `analog@phaser` with the HB100 on the
bench, all four fixes installed and `Rx_gain = 30`:

    points 162
    min -19.53  max 9.03  range 28.56 dB
    peak at -9.2 deg

28.56 dB of pattern where there had been a flat line, slightly better than the
simulator predicted. The peak sits at -9.2 deg rather than boresight simply
because that is where the horn was pointing; the sim pins its target at 0 deg,
the bench does not.

## The Pi

`analog@phaser`, armv7l (32-bit), Python 3.9, install dir
`/home/analog/pyadi-iio/examples/phaser`. Deployed with `install.sh`, which
runs ON the Pi. Claude Code cannot be installed on it: no
32-bit ARM binary exists. Drive it over ssh from elsewhere.

Its startup log before this branch was installed:

    Rx_gain: 1                                            <-- needs to be 30
    Phase cal: [0.0, -28.125, -22.5, 0.0, 109.6875, 120.9375, -163.125, -101.25]
    Gain cal:  [0.967, 1.0, 0.940, 0.984, 0.503, 0.502, 0.508, 0.515]
    Channel cal: [0.0, 1.2555]
    Initializing external LO ... (ADF4159 register 3156130371 Hz, /4)

Two things to read from that. The LO line has no `WARNING: ADF4159 readback`
after it, so the PLL is locked and the LO fix from `main` is good. And the
calibration is populated and *large* -- corrections over 160 degrees on
elements 7 and 8. Discarding those, which is what the old code did, cannot
produce a coherent beam. No need to re-run calibration; it just needs the code
that uses it.

## Do this

On the Pi, as `analog`:

    cd /home/analog/pyadi-iio/examples/phaser
    sed -i 's/^Rx_gain = .*/Rx_gain = 30/' config.py
    export PHASER_REF=claude/hb100-beam-pattern-display-3ud5v3
    curl -fsSL https://raw.githubusercontent.com/livethisdream/phaser/main/install.sh | bash

`config.py` first: `install.sh` keeps an existing one and restarts the service
at the end, so one run picks up both changes.

Then confirm it landed:

    grep -c latch_rx_settings ADAR_pyadi_functions.py   # 2
    grep -c rx_vm_enable ADAR_pyadi_functions.py        # 2
    grep -c _apply_phase_cal phaser_headless.py         # 3
    grep '^Rx_gain' config.py                           # Rx_gain = 30
    journalctl -u phaser-headless --since "1 min ago" --no-pager

Startup should now say `Rx_gain: 30`. Then open `http://<pi-ip>:8080` and
hard-refresh.

## What "working" looks like

A main lobe near boresight with sidelobes clearly below it. Given this array's
pcal runs past 160 degrees, the change should be obvious rather than subtle.

If it is still flat after all of the above, it is something not yet found. Get
the sweep data itself rather than the log -- `ArrayGain` and `ArrayAngle` off
the WebSocket, or a screenshot of the plot. The useful question is whether the
gain values vary at all across angle, and by how much.

## Rx gain: turn it DOWN, not up

Counterintuitive enough to be worth writing down: this bench got 10 dB more
beam pattern by *reducing* Rx gain. Measured 2026-08-27 by stepping
`set_rx_gain` live over the WebSocket:

     gain    peak     null    range   peak@
        5    -5.01   -44.19   39.18    -5.5 deg
       10    -0.02   -39.44   39.42    -4.6 deg
       15    +4.28   -35.72   40.00    -3.7 deg
       20    +7.58   -30.63   38.21    -5.5 deg
       25    +9.03   -27.17   36.20    -6.5 deg
       30    +9.03   -20.45   29.48    -9.2 deg

Read the peak column as deltas per 5 dB step: +4.99, +4.30, +3.30, +1.45,
+0.00. The nulls track the gain all the way up; the peak stops. By 30 the
converter is pinned -- 5 dB more gain moves the peak not at all -- and the
clipped, flattened main lobe also drags the reported peak angle out to -9.2 deg.

**`Rx_gain = 10`** is the setting. Not 15, even though 15 shows the largest
range: 39.18 / 39.42 / 40.00 across 5/10/15 is a 0.8 dB spread on a metric
built from one time-domain peak sample at `Averages = 1`, which is inside
run-to-run scatter. Gain 10 is the last point where the peak tracks gain
exactly (+4.99 for +5.00), so it is the real edge of the linear region, and it
leaves 5 dB of headroom for a horn moved closer.

Note this is bench geometry, not a universal number -- it depends on how far
the HB100 sits from the array. The repo's `config.py` still ships
`Rx_gain = 30`, which hard-clips on this bench. That default is only used to
seed a Pi that has no config at all, so it has been left alone, but it is worth
a decision.

The `peak@` column scatters about 2 degrees within the linear region. That is
measurement noise; `Averages` is 1. Raising it to 4 tightens both the pattern
and the peak-angle estimate, at the cost of sweep rate.

## Known cosmetic issue, deliberately not fixed

Every restart logs `Exception in thread Thread-1` from `_command_loop`:
`stop()` calls `ctx.term()` while that thread sits in `poller.poll(100)`. It is
shutdown noise, unrelated to the beam pattern, and was left alone to keep the
diagnosis clean. Fix is to signal the loop and join it before terminating the
context.
