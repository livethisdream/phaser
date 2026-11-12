# SAR mode: plan

Status: draft for review, 2026-11-12. No code until the open decisions below
are settled. FMCW (C5) passed on phaser-01 on 2026-11-11, which this builds on.

## What it is

Synthetic-aperture radar (SAR): slide the kit along a straight line, take one
FMCW measurement at each position, and combine them coherently into a
top-down image of the scene: range (out from the array) by cross-range (along
the rail). One look sees range only; the slide adds the second dimension at a
resolution no single antenna this size could give.

The Phaser is well suited to it: 500 MHz chirps already give 0.30 m range
resolution, the chirps are coherent (0.16 deg phase std chirp to chirp,
phaser-01 2026-11-11), and its electronically steered array allows
spotlight SAR, which a fixed-antenna kit can't do.

## The numbers

At 10.25 GHz the wavelength is 29.2 mm.

| | |
|---|---|
| Range resolution | c / 2B = 0.30 m at 500 MHz (sub-bin peak reading does not improve resolution) |
| Array aperture D | 8 elements x 14 mm = 112 mm, beam ~15 deg wide at boresight |
| Stripmap cross-range resolution | ~D/2 = 5.6 cm, whatever the range or rail length, because a target is lit only while it is inside the beam |
| Spotlight cross-range resolution | lambda R / 2L: 1 m of rail at 2 m gives 2.9 cm; 0.5 m gives 5.8 cm |
| Position step, stripmap | <= lambda / (4 sin(beam/2)) ~ 5 cm with the full array; finer with a wider beam |
| Position step, spotlight | <= lambda / 4 = 7.3 mm for alias-free cross-range at the scene |
| Position accuracy | ~1 mm (a few percent of lambda) for a focused image; errors defocus, they don't shift |

Spotlight mode steers the beam at the scene center from every position, so a
target stays lit across the whole rail: finer resolution, at the cost of more
positions and a smaller imaged patch. Stripmap keeps boresight and images a
strip as long as the rail.

## How an image is formed

1. At each position n, record a burst (default: the Range + Velocity chirp,
   averaged over its 256 chirps for ~24 dB more SNR; the scene is still).
2. Range-compress: the range FFT of the averaged chirp, zero-padded 4x so the
   backprojection can interpolate.
3. Phase reference: take the 0 m leakage's phase at each position and remove
   it, so drift between positions (seconds apart, separate bursts) cancels.
   The leakage path doesn't move with the kit, so it is a good reference.
4. Backproject: for each image pixel p, sum over positions the range-compressed
   sample at R_n = |p - x_n| times exp(+j 4 pi R_n / lambda). That is
   N_pixels x N_positions operations: 200 x 200 pixels x 200 positions = 8 M,
   about a second in numpy on the Pi, and fine in the browser simulator.
5. Show |image| in dB on a new SAR Image tab, rebuilt after every position so
   the picture sharpens as the slide goes.

Backprojection rather than a frequency-domain (omega-k) algorithm: it handles
any rail positions, uneven steps, near range and spotlight steering with one
formula, and it is what a student can follow.

## Phases (each its own PR into radar-dev)

**S1. Simulator first, no hardware.**
- A SAR scene: point scatterers (corner reflectors) at known (x, y), and the
  array's position as a parameter of the FMCW synthesis (today it sits at the
  origin).
- Backprojection in Python (`phaser_radar_dsp.sar_backproject`) and JS, with
  parity cases, as for every other DSP function.
- The radar UI's SAR section (rail length, step, stripmap or spotlight,
  Capture Position, Clear) and the SAR Image tab. In simulation, an
  "Auto slide" button steps the simulated kit along the rail.
- Save / Load a SAR dataset: an .npz of iq per position, the positions, and
  the usual fs, fc, B, T_ramp, f0.
- Works on the Pages demo.
- Done when: two simulated corners 10 cm apart in cross-range at 2 m are
  resolved in spotlight mode with 1 m of rail, and parity holds.

**S2. On the kit, stop-and-go.**
- Capture Position takes one averaged burst at the current position. The
  position comes from the step count (n x step) by default, or typed in.
- The user slides the kit by hand along a ruler or a taped rail.
- Spotlight steering per position uses the existing beam steering.
- Done when: two real corners 10-15 cm apart at ~2 m show as two spots.

**S3. Optional: motion hardware.**
- A motorized linear rail (a stepper on the Pi's GPIO, or a USB controller),
  so a slide runs unattended and repeatably.
- Or ISAR instead: the kit still, the target on a turntable (the fan and
  spinner already rotate), imaging the object rather than the room.

## Open decisions

| Decision | Options | Recommendation |
|---|---|---|
| Positioning on the kit | ruler stop-and-go / motorized rail / turntable (ISAR) | Ruler stop-and-go for S2. It costs nothing and shows whether the phase reference holds before buying anything |
| Mode first | stripmap / spotlight | Spotlight: finer resolution from the same rail, and it uses the phased array, which is the point of this kit. Stripmap is a checkbox later |
| Audience | course lab / demo | Decides how much the UI explains (resolution, step size, grating lobes). If it's a lab, it needs a short procedure doc alongside |
| Rail length and step | 0.5 m / 1 m; step from the table above | 1 m at 7 mm in spotlight is ~140 positions, minutes by hand. 0.5 m at 7 mm (~70) for a first test |
| Image size | patch around a chosen center / full strip | A 1 x 1 m patch at a chosen range (default 2 m), 5 mm pixels |

## Risks

- Phase drift between positions larger than the leakage reference can remove,
  e.g. if the burst-to-burst tone phase isn't constant. S2's first test should
  image a single still corner while the kit stays put: it must focus to one
  spot.
- Position error from a hand slide: 1-2 mm is plausible with a taped rule,
  enough to soften but not destroy the image. Autofocus is out of scope.
- Room multipath (walls, floor) will show as extra spots; a field or a large
  room helps on the first real image.

## Not in this plan

Pulse-Doppler (as a simulator teaching mode) is a separate plan, after SAR.
