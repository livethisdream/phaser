# SAR mode: plan

Status: S1 (simulator, ISAR) in progress, 2026-11-12. S3 (motorized
turntable) is deferred: not this semester. FMCW (C5) passed on phaser-01 on
2026-11-11, which this builds on.

## What it is

Synthetic-aperture radar: take FMCW measurements from many viewing geometries
and combine them coherently into a 2D image, range by cross-range, at a
cross-range resolution no single antenna this size could give. Two ways to get
the geometries, sharing one image-formation algorithm:

- **ISAR (inverse SAR):** the radar stands still and the **target rotates**,
  on the printed turntable already in the repo (`hardware/stand`,
  `hardware/stand-simple`). It images the object on the platter.
- **SAR:** the **radar moves** along a straight rail past a still scene. It
  images the scene, such as a room or corner reflectors on the floor.

The Phaser suits both: 500 MHz chirps give 0.30 m range resolution, the
chirps are coherent (0.16 deg phase std chirp to chirp, phaser-01
2026-11-11), and its steerable array allows spotlight SAR.

**Order: ISAR first.** It needs no new hardware. The stand already exists, and
its 5 deg detents are fine enough (see below) to start by hand. Its GT2 rim
then takes a stepper for an automatic sweep. SAR on a printed rail comes
second.

## The numbers

At 10.25 GHz the wavelength is 29.2 mm. Range resolution is c / 2B = 0.30 m
at 500 MHz for both.

### ISAR on the turntable

| | |
|---|---|
| Cross-range resolution | lambda / (2 x total rotation): 30 deg gives 2.8 cm, 60 deg 1.4 cm |
| Angle step | <= lambda / (4 r), r = the target's radius from the axis: 5 deg suits r <= 8 cm, 2 deg r <= 21 cm |
| Angle accuracy | ~0.1 deg for a focused image; the stand's detents and a stepper both beat it |
| Radar distance | 1-2 m from the axis, on its own tripod, beam centered on the platter |

The stand's 5 deg detents give a first test with no motor: 12 clicks over 60 deg
image targets within 8 cm of the axis (a few corner reflectors close together)
at about 1.4 cm across.

### SAR on a rail

| | |
|---|---|
| Array aperture D | 8 elements x 14 mm = 112 mm, beam ~15 deg wide |
| Stripmap cross-range resolution | ~D/2 = 5.6 cm, whatever the range or rail length |
| Spotlight cross-range resolution | lambda R / 2L: 1 m of rail at 2 m gives 2.9 cm |
| Position step | spotlight <= lambda/4 = 7.3 mm; stripmap ~5 cm |
| Position accuracy | ~1 mm for a focused image |

## How an image is formed

The same steps for both; only the geometry in step 4 differs.

1. At each angle or position, record one burst (the Range + Velocity chirp,
   averaged over its 256 chirps for ~24 dB more SNR; the scene holds still
   between steps).
2. Range-compress: the range FFT of the averaged chirp, zero-padded 4x for
   interpolation.
3. Phase reference: remove the 0 m leakage's phase at each step, so drift
   between bursts (seconds apart) cancels. The leakage path is inside the
   kit, so it doesn't change as the target turns or the kit slides.
4. Backproject: for each image pixel p, sum over steps the range-compressed
   sample at that step's range to p, times exp(+j 4 pi R / lambda).
   - ISAR: pixels sit on the platter and rotate with it; R is from the fixed
     radar to the pixel rotated by that step's angle.
   - SAR: R = |p - x_n|, from the rail position.
   200 x 200 pixels x 100 steps is 4 M operations: about a second in numpy
   on the Pi, and fine in the browser.
5. Show |image| in dB on a new **SAR Image** tab, rebuilt after every step so
   the picture sharpens as the sweep goes.
6. ISAR only: still clutter (the room, the stand's own base) doesn't rotate
   with the target, so it smears into a ring. Subtracting the mean over all
   angles from each step's range profile removes it, the same idea as MTI.

Backprojection rather than a frequency-domain algorithm (polar format,
omega-k): one formula for any geometry, uneven steps and near range, and it
is what a student can follow.

## Phases (each its own PR into radar-dev)

**S1. Simulator, both geometries.** ISAR part done, 2026-11-12: the
`turntable` scene, `phaser_radar_dsp.isar_profile` / `isar_backproject`
(Python and JS, parity in `tests/test_radar_sim_parity.py`), the headless
`isar_capture` / `isar_image` / `isar_clear` commands, and a SAR (Turntable)
section plus an ISAR Image tab in the radar UI. Two corners 3 cm apart
resolve with 60 deg (`tests/test_isar.py`). Still to come: the SAR rail
scene and Save / Load of a step set. What the simulator showed:
- 60 deg resolves across (1.4 cm) but barely down-range (~11 cm): the
  image is streaks. A full 360 deg turn gives ~7 mm both ways, so the
  simulated Sweep defaults to 360 deg.
- 5 deg steps image cleanly only within ~8 cm of the axis (grating lobes
  at lambda / (2 x step) = 17 cm), so the patch defaults to 16 cm.
- Remove Still Clutter is off by default: it also removes a target on the
  axis, which S2's first test uses.
- Scenes: point targets on a rotating platter (ISAR), and point targets in a
  room with the radar's position as a parameter of the FMCW synthesis (SAR).
- `phaser_radar_dsp.sar_backproject` in Python and JS, with parity cases.
- A SAR section in the radar UI: geometry (turntable or rail), angle or
  position step, sweep length, Capture Step, Clear, plus Auto Sweep in
  simulation. The SAR Image tab.
- Save / Load a SAR dataset: an .npz with iq per step, the angles or
  positions, and the usual fs, fc, B, T_ramp, f0.
- Works on the Pages demo.
- Done when: two simulated corners 3 cm apart on the platter resolve with
  60 deg of rotation, two 10 cm apart resolve on 1 m of rail in spotlight,
  and parity holds.

**S2. ISAR on the kit, by hand.**
- Targets on the stand's platter (the tripod platter, mast removed); the
  Phaser on its own tripod 1-2 m away.
- Click the platter one 5 deg detent, press Capture Step, repeat. The angle
  is step count x 5 deg, or typed in.
- First test: a single corner on the axis must focus to one spot. That checks
  the phase reference before anything else.
- Second test: one corner off the axis, on a known side. The image's
  handedness depends on the sign of the beat phase, which the simulator
  and the kit may not share (the simulator's Doppler sign is opposite the
  kit's: it reads an approaching target as positive). A mirrored image
  means flip the sign in `isar_backproject`.
- Done when: two corners about 5 cm apart on the platter show as two spots.

**S3. Motorized turntable. Deferred: not this semester (2026-11-12).**
- A stepper on the stand's GT2 rim (300 teeth; with a 20T pulley 15:1), on
  the bracket mounts the stand already has. The Pi steps it and captures at
  each angle, so a sweep runs unattended.
- A small printed bracket and pulley mount (OpenSCAD, in `hardware/stand`).

**S4. SAR on a printed rail.**
- A quick manual test first: a masking-tape line marked every 7 mm, the kit
  slid by hand, about 70 captures over 0.5 m.
- Then a printed indexed rail (OpenSCAD, new `hardware/sar-rail/`): a
  carriage on joined segments or an aluminum extrusion, detents every 7.3 mm,
  and the same stepper and belt as S3 for unattended sweeps.

## Open decisions

| Decision | Options | Recommendation |
|---|---|---|
| Stepper and driver | 28BYJ-48 + ULN2003 (~$5, slow, 5 mm shaft) / NEMA17 + A4988 or TMC2209 (the stand's design assumes NEMA17) | NEMA17 + TMC2209: what the stand was drawn for, and quiet |
| How the Pi drives it | Pi GPIO pins (need to check which the Phaser's own GPIO use leaves free) / a USB stepper controller | Check the free GPIO first; USB only if there aren't any |
| Audience | course lab / demo | Decides how much the UI explains (resolution, step size, why still clutter rings). A lab needs a short procedure doc too |
| Rail length (S4) | 0.5 m / 1 m | 0.5 m first: ~70 positions in spotlight |
| Image patch | ISAR: the platter, ~0.3 m across, 2 mm pixels; SAR: 1 x 1 m at a chosen range, 5 mm pixels | As listed |

## Risks

- Phase drift between bursts larger than the leakage reference removes. S2's
  first test (one corner on the axis) shows it straight away.
- The stand's own plastic and screws turn with the target and will image
  too; corner reflectors are far brighter, and a sheet of absorber or a
  wooden spacer under the targets helps.
- Room multipath shows as extra spots, more in SAR than ISAR.

## Not in this plan

Pulse-Doppler (as a simulator teaching mode) is a separate plan, after SAR.
