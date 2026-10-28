# Why calibration goes deaf, and the six bugs behind it

Every failure in this document looks identical from the console: calibration
reports that no signal is reaching the array, or it completes and writes
numbers that are noise. None of them is detectable at runtime. The attribute
writes are accepted, the readbacks agree, and MUXOUT is not wired as lock
detect on this board — it reads 0 at every LO, including one demonstrably
producing a 43 dB beam pattern. There is nothing to catch.

**Three of the six come from the upstream ADI examples**, so they are waiting
in any kit built from
[`pyadi-iio/examples/phaser`](https://github.com/analogdevicesinc/pyadi-iio/tree/main/examples/phaser),
not just from this repo. The other three are ours. All six are fixed here; this
document exists so the next person who sees a flat beam pattern can tell which
of them they are looking at, and so the reasoning survives the people who did
it.

| # | Bug | Origin | Symptom |
| --- | --- | --- | --- |
| 1 | IF above this unit's ceiling | ADI default + unit variation | Cal aborts: "no signal is reaching the array" |
| 2 | Search and cal run different IFs | ADI | Search finds a 75 dB tone; cal is deaf on the same kit, same minute |
| 3 | Captures return the previous tuning | ADI | HB100 search lands ~8 MHz off a real tone |
| 4 | Phase cal takes an argmax, not a null | **this repo** | Cal completes; reruns disagree by 104° |
| 5 | Radar modes inherit unwritten phases | **this repo** | After a restart, radar sees only 0 m leakage; upstream on the same kit sees targets |
| 6 | FMCW exit leaves the TDD gating the Rx DMA | **this repo** | After any FMCW session, the next new Rx buffer never fills: Errno 110, or a hung backend |

---

## 1. The IF ceiling is per unit, and the shipped default is past it

`config.py` ships `Rx_freq = int(2.2e9)`. On kit `phaser-00` that is 20 MHz
past where the receive chain stops working.

```
IF 2.175 GHz →  -2.95 dBFS,  85.4 dB SNR
IF 2.180 GHz →  -2.95 dBFS,  84.9 dB SNR
IF 2.185 GHz →  -6.80 dBFS,  74.2 dB SNR
IF 2.195 GHz → -36.58 dBFS,  52.1 dB SNR
IF 2.200 GHz → -74.83 dBFS,  16.1 dB SNR   ← config.py ships this
```

It is an analog band edge, not a PLL fault: the rolloff is graded rather than
a step, it shows no hysteresis between up and down sweeps, and none of seven
relock attempts recovered it (rewrite, `ramp_mode`, `powerdown` 1→0,
staircase approach, approach from above, full `configure()`).

**The ceiling differs by ~200 MHz between units.** Two measured so far:

| kit | IF clean to | IF dead by |
| --- | --- | --- |
| phaser | ~2.37 GHz | 2.45 GHz |
| phaser-00 | 2.18 GHz | 2.20 GHz |

So no constant is right everywhere, and `config.py` is site-owned and
deliberately never deployed over — meaning a hand-edit that rescues one kit
cannot travel to the next. That is exactly what happened: kit "phaser" was
found carrying 1.9 GHz, and the repo default stayed 2.2.

**Fix.** `phaser_characterize_headless.py` sweeps the IF against the known
HB100 and stores the usable window in `calibration.json`, which *does* deploy.
`phaser_functions.rx_freq_source()` resolves measurement → `Rx_freq_override`
→ `config.Rx_freq` → bootstrap default, so a measurement always beats a
constant. Run it **once per kit**, on a bench with the HB100 aimed at the
array:

```
python3 phaser_find_hb100_headless.py        # 1. find the source
python3 phaser_characterize_headless.py      # 2. find the IF   ← once per kit
python3 phaser_cal_headless.py               # 3. channel, gain, phase
```

The sidebar has the same three as **Find HB100 / Characterize IF /
Calibrate**, in that order, and Characterize asks for confirmation first
because it is worthless unless the HB100 is actually set up. The sidebar also
shows the IF in use and whether it was measured on this kit — an unverified
IF is otherwise indistinguishable on screen from a working one.

**Calibration recovers from this on its own, once.** If the contrast gate
fails on a unit that has never been characterized, cal runs the sweep itself,
saves the result, retunes and retries — because the bench conditions a sweep
needs are exactly the ones calibration needs, so anyone who got that far has
already set them up. Three limits keep that safe. A unit that *already* has a
measurement is never re-swept (there a failed gate means a dead source or a
cable, and sweeping then would overwrite good data with bad). A sweep that
hears nothing saves nothing. And a kit whose **HB100 frequency has never been
measured** is not swept at all — the sweep tracks the LO against the stored
source frequency to keep the tone in the analysis window, so against
`config.py`'s 10.525 GHz nominal it tracks the wrong frequency and hears
nothing at every IF. That would read as "the IF was not the problem" when the
IF was never tested. **Run the HB100 search first** — which is why the order
is an order. If the recovery runs and still hears nothing at
any IF, the abort says so and stops recommending characterization, since it
has just been ruled out.

Characterization is deliberately **not** part of `provision.sh`. Provisioning
runs unattended at first boot (`scripts/pi/firstrun.sh` passes `--yes`), often
before the kit is assembled and with no HB100 in front of it; a sweep then
would measure noise. `install.sh` prints the three steps instead when it
finds no measurement, and refusing to save a sweep that heard nothing means
a mistaken run is safe rather than silently destructive.

### Why the chosen IF is the middle of the window

The top edge is the thing that varies between units, so distance from it is
the only margin not borrowed from another kit. phaser-00 measured clean over
1.200–2.175 GHz and the routine chose **1.690 GHz**.

That number has independent corroboration. ADI's own production test —
[`phaser_prod_tst.py:98`](https://github.com/analogdevicesinc/pyadi-iio/blob/main/examples/phaser/phaser_prod_tst.py),
the script that decides whether a board passes — runs `rx_lo = 1.9e9` against
`SignalFreq = 10.2e9`, an **LO of 12.1 GHz**. The characterization picked
1.690 GHz against a 10.40769 GHz source: **LO 12.098 GHz**, within 2 MHz of
the factory acceptance point, arrived at from a blind sweep with no knowledge
of that file. Two unrelated methods landing together is good evidence that
~12.1 GHz is where this chain actually wants to sit.

---

## 2. Upstream picks four different IFs

| upstream file | IF |
| --- | --- |
| `config.py` | 2.2 GHz |
| `phaser_examples.py:138` (the cal example) | `config.Rx_freq` → 2.2 GHz |
| `phaser_minimal_example.py:145` | 2.2 GHz hardcoded |
| `phaser_find_hb100.py:103` | 2.0 GHz hardcoded |
| `phaser_prod_tst.py:98` | 1.9 GHz hardcoded |

This is why the failure is so confusing in practice. On a kit whose ceiling
falls between 2.0 and 2.2 GHz, **the HB100 search works and calibration does
not** — same kit, same bench, minutes apart. Everything then points at the
calibration code, which is innocent.

**Fix.** One resolver, `rx_freq_source()`, used by `phaser_cal_headless.py`,
`phaser_find_hb100_headless.py`, `phaser_headless.py`, `phaser_service.py`
and `install.sh`. This repo previously had the same disease in milder form —
`2.2e9` and `2.4e9` as separate fallbacks in `phaser_service.py`, plus the
inherited hardcoded `2.0e9` in the search.

---

## 3. A capture returns the tuning before last

Upstream discards a buffer in two of the four places it reads one:

```
phaser_examples.py:261-262     data = my_sdr.rx(); data = my_sdr.rx()   discard + read
phaser_prod_tst.py:359-360     data = my_sdr.rx(); data = my_sdr.rx()   discard + read
phaser_find_hb100.py:154       data = my_sdr.rx()                       single
phaser_minimal_example.py:159  data = my_sdr.rx()                       single
```

In the HB100 search, every 10 MHz step was therefore labeled with the *next*
step's frequency, putting the candidate ~8 MHz above a real 74 dB tone — which
the search's own LO-shift check then correctly rejected, reporting failure on
a perfectly good source.

Settle time is not the variable. Measured at 0.05, 0.2 and 1.0 s with
identical results; discarding two buffers did not help either. Only
`rx_destroy_buffer()` fixed it.

The same two files also hand `spec_est` a hardcoded sample rate
(`30000000`, `30e6`) while `LTE20_MHz.ftr` moves the radio to **30.72 MSPS** —
a 2.4% error in every baseband offset. Both also pass `ref=2 ^ 12`, which is
XOR: it evaluates to 14, not 4096.

**Fix.** `_flush_rx()` before every capture that follows a state change, and
`int(sdr.sample_rate)` for the frequency axis.

---

## 4. Phase calibration took an argmax — this one is ours

Upstream finds the **null**:

```python
ph_delta = to_sup((180 - PhaseValues[gain.index(min(gain))]) % 360.0)
```

That is the right instinct. Two elements summing give a response proportional
to `|1 + exp(jΔ)|`: the peak is quadratic and the null is sharp, so the null
carries almost all of the phase information.

`b66125a` deleted this repo's copies of the calibration functions and
`c032f89` reimplemented them — with `np.argmax`. Taking the maximum of a
quadratic peak means **20° of error costs 0.26 dB**, so a couple of tenths of
a dB of noise moves the answer tens of degrees. Each pair's result is then
held while the next pair sweeps, so it accumulates along the array.

Five runs on a healthy bench (85 dB SNR, 33 dB array contrast):

| element | argmax std | fit std |
| --- | --- | --- |
| 2 | 17.3° | 0.55° |
| 5 | 21.6° | 2.33° |
| 8 | 43.6° | 2.01° |
| **mean** | **27.5°** | **2.00°** |

**Fix**, and why not simply restore the null: upstream's null-finding is
undermined by its own code.

```python
s_mag_sum = np.max(s_sum[peak_bin - width : peak_bin + width])
s_mag_sum = np.max(s_sum)      # overwrites the line above
```

The second line throws away the bin restriction whose stated purpose, in the
comment four lines up, is *"This prevents detecting other spurs when deep in a
null."* Deep in a null the fundamental is tiny, so a whole-spectrum maximum
latches onto a spur — precisely when the method depends on it not doing that.

So this repo fits the whole curve instead:

```
φ₀ = angle( Σ P_linear · e^{jφ} )
```

the first Fourier coefficient of a known one-parameter family. It needs
neither a resolved null nor bin tracking, it is immune to an additive pedestal
(a constant is orthogonal to `e^{jφ}`, and the measured response falls to the
noise floor rather than to zero), and it is continuous, so it resolves
sub-degree on a sweep coarser than the ADAR1000's own 2.8125° step.

It also yields a free quality metric, `|c1|/|c0|` — about 0.33 for a clean
response and near zero for a flat one. Measured 0.312–0.335 on every pair.
That is the first warning this code has had that a sweep carried no phase
information at all; an argmax over noise simply returned a confident number.

### Why the test suite never caught it

`FakeArray` in `tests/test_calibration_math.py` was noiseless, and on
noiseless data argmax and a fit both land within one step. It now takes an
optional noise term, defaulted off, and
`tests/test_phase_estimator.py` compares the two estimators at the ~0.4 dB
noise level that reproduces the bench's per-pair scatter.

---

## 5. Radar modes listened through phases nothing had written — ours too

Only the beamforming sweep writes the ADAR1000 Rx phases (`do_sweep` ->
`ADAR_set_Phase`). Startup wrote the taper and left the phases alone, and the
CW and FMCW modes wrote the taper and inherited whatever phases were there.
After a service restart, with no sweep yet, that is nothing at all.

On phaser-01 the result was an array that heard nothing but its own Tx->Rx
leakage. FMCW reported every frame at 0 m, and with 2-pulse MTI at a fixed
+0.24 m/s and 1.8 dB, while a metal pot swung at ~1.8 m in front of it. The
upstream `Range_Doppler_Plot.py` on the same kit, at the same IF, saw the pot.
It calls `set_chan_phase(i, 0)` for every element before it captures.

Writing boresight (the phase cal alone) and latching it from a second process,
mid-run, changed the next frame:

| Phases | Peak range | Velocity | Peak |
| --- | --- | --- | --- |
| As inherited (read back as all 0) | 0.01 m, every frame | +0.24 m/s, fixed | 1.8 dB |
| Boresight + phase cal, latched | **1.68 m** (pot at ~1.83 m) | −2 to +8 m/s, tracking the swing | 10–21 dB |

Note the readback: the inherited phases read **0**, which is boresight. A
readback cannot tell a written 0 from registers that were never written, so
"the phases say 0" proves nothing here. The likeliest explanation is the
power-on state of the ADAR1000 Rx vector-modulator registers, which leaves the
channel close to null until a phase is written; that is not yet confirmed
(see Open).

The fix: `phaser_cw_radar.apply_boresight()` writes and latches the phase cal,
and is called at startup and on entering either radar mode. Unlike
`apply_taper`, it raises on a failed write, so the mode refuses to start
instead of running deaf. Exit does not restore phases; the sweep rewrites all
eight on every angle. `tests/test_boresight_phases.py` asserts on the stub's
*latched* phase, so a write without a latch fails it.

## 6. FMCW exit left the TDD holding the Rx DMA — ours

Synced FMCW enables the Pluto's TDD engine and sets every channel's polarity
to 0. With the engine off, each channel idles at its polarity, and channel 1
low stops the Rx DMA. Our exit only wrote `enable = False`, so after any FMCW
session the Pluto was left unable to fill an Rx buffer. The state lives on the
Pluto, so it outlived the process and survived service restarts.

It went unseen until the Rx buffer was rebuilt. pyadi creates the buffer once
and keeps it, and nothing ever destroyed it, so every capture after startup
reused a buffer created before the gate closed. Once mode changes rebuilt it
(so that `rx_buffer_size` took effect), CW after FMCW hung on its first
capture. CW set the libiio timeout to 0, which is "forever", so the main loop,
the WebSocket and both UIs went with it.

On phaser-01, a plain capture with a fresh buffer and a 3 s timeout:

| TDD state (engine off) | Capture |
| --- | --- |
| As our exit left it: channels enabled, all polarity 0 | Errno 110 |
| Channels disabled | Errno 110 |
| `sync_external = 0`, enable toggled | Errno 110 |
| Channel 1 polarity 1, enable toggled to latch | **OK, 20 ms** |
| Channel 1 back to 0 / back to 1 | Errno 110 / OK |

Upstream's `end_program` does exactly this: channel 1 polarity to
`not phaser_enable`, channel 2 to `phaser_enable`, then `enable` True and
False. The fix, `phaser_fmcw_radar.release_tdd()`, does the same on exit and
again at startup, because a session that crashed, was killed, or was a lab
script leaves the same state behind. CW's libiio timeout is now 5 s, so a
stuck capture raises instead of hanging the backend.

---

## How these were separated

Worth keeping, because the same confound will recur.

**One axis at a time.** `LO = SignalFreq + Rx_freq` with the source fixed, so
tuning normally moves the LO and the IF *together* and no ordinary sweep can
say which one a limit is on. An earlier sweep recorded an "LO ceiling" that
was really an IF ceiling for exactly this reason.

Park the Pluto's `rx_lo` and move only the CN0566 LO. The tone then walks
across baseband instead of the receiver retuning, and ten MHz of LO fits
inside the analog bandwidth:

```
baseband offset = (LO − SignalFreq) − rx_lo
```

| rx_lo parked | LO swept | tone tracked | peak |
| --- | --- | --- | --- |
| 2.0 GHz | 12.398–12.408 GHz | 11 of 11 | −2.3 to −3.1 dBFS |
| 2.2 GHz | 12.598–12.608 GHz | 0 of 11 | −75 dBFS, noise |

The ADF4159 received cleanly at 3.14691 GHz and nothing at 3.14950 — 2.6 MHz
apart, opposite outcomes, differing only in `rx_lo`.

**Contrast, not level.** Switching all eight elements off cannot attenuate a
spur generated after the mixer, so the on/off difference separates "the array
is receiving" from "something inside the receiver is loud" for the price of
two captures. That is the gate in `measure_array_contrast`.

The probes that did this live in [`tests/hardware/`](../tests/hardware/) with
a README. They run on the Pi, save nothing, and are not collected by pytest.

---

## Reading a failure

Calibration's abort now prints the chain it ran on, which is the part nobody
can check by looking at the bench:

```
The chain this ran on:
  HB100    10.407690 GHz
  IF       1.690 GHz      (calibration.json (measured))
  LO       12.098 GHz     = HB100 + IF
  ADF4159  3.0244 GHz     = LO / 4
```

| What you see | Look at |
| --- | --- |
| Cal warns the HB100 frequency is nominal | Run Find HB100. Nothing downstream works until the source frequency is real |
| Cal aborts, contrast < 6 dB, IF source is `config.py` or a default | Bug 1 — cal should have self-recovered; if it did not, check it reached the sweep |
| Cal sweeps every IF and hears nothing at any of them | Not the IF. Dead source, or cables |
| Cal aborts but the HB100 search works | Bug 2 — they are on different IFs |
| Search reports the peak "moved with the LO" | Bug 3 — check the flush is present |
| Cal completes, reruns disagree by tens of degrees | Bug 4 — check the fit quality line |
| Cal completes, beam pattern is flat | Phase cal ran against an off-boresight source |
| Radar peaks pinned at 0 m, MTI shows nothing moving | Bug 5 — check `Boresight phases latched` in the log |
| Every capture times out (Errno 110), or the UI disconnects after a radar mode | Bug 6 — check `Pluto TDD released` in the log, and `iio_attr -u ip:192.168.2.1 -c iio-axi-tdd-0 channel1 polarity` reads 1 |

---

## Open

- **The 4↔5 element phase step.** phaser-00 measures ~142° there. That is the
  boundary where the array splits across the two Pluto channels, and ADI gives
  it extra tolerance for that reason — `phase_cal_limits = [90, 90, 90, 120,
  90, 90, 90]` in `phaser_prod_tst.py`. So a large step is expected, but 142°
  is past their 120° limit and worth re-measuring.
- **Boresight.** Phase calibration assumes the source is at mechanical
  boresight; cal prints that as an instruction. An off-boresight source bakes
  a steering term into `pcal`. The HB100 was disturbed during the session that
  produced the numbers above, so phaser-00 should be re-calibrated with the
  source re-aimed before its `pcal` is trusted.
- **Whether the IF edge is the AD9361 or the CN0566's IF path** is untested.
  It does not change the fix — both are per-kit — but it would say whether
  swapping a Pluto moves the window.
- **Only two kits have been characterized.** The ~200 MHz spread between them
  is the entire evidence base for treating the ceiling as per-unit. A third
  measurement would be worth more than any further reasoning here.
- **What the never-written phase state actually is.** Bug 5 is fixed by
  writing the phases, which is right whatever the cause. Confirming it means
  restarting the service and reading the raw Rx I/Q registers on each chip
  before and after the first write.
