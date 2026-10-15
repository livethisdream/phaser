---
name: "#phasergui"
dateCreated: 2026-08-06
dateModified: 2026-09-10
container: cdocker
---
# Overview

Conversion of the legacy `phaser_gui.py` (from pyadi-iio examples) into a headless, browser-accessible architecture for the CN0566 Phaser kit. The runtime lives on a Raspberry Pi (`phaser.local` / 192.168.86.20) exposing an HTTP server on port 8080 for the static frontend and a WebSocket server on 8765 for real-time sweep data and commands. Frontend is vanilla JS + Plotly.js bundled with Vite. The project spans two feature streams: digital beamforming (this note's focus) on `main`, and a radar app under `frontend-radar/` being built out CW-first, FMCW-second on `radar-dev`.

# Special Instructions

- Don't remove or delete anything without explicit approval.
- Ask clarifying questions before making assumptions.
- Plan before executing — expect back-and-forth before code changes land.
- Verify each memory's file:line citations against current code before asserting as fact. The beamforming memories date from 2026-06-25 and the Tauri one from 2026-05-22; only the repo-hygiene notes are current.
- **Branch hygiene:** resolved 2026-08-25. `browser-based` was renamed to `main` (now the default branch), and `radar-dev` was split off for radar work; CI builds `dist/` on both. Radar changes (`frontend-radar/`, `phaser_cw_radar.py`, FMCW, range-Doppler, CW radar UI) belong on `radar-dev`.

# Traps

- **The ADF4159 accepts an out-of-range LO write silently.** Writing 12.625e9 to
  `synth.frequency` is accepted and reads straight back; the PLL simply cannot
  lock and the array receives noise. The only symptom is an empty spectrum.
  `SDR_LO_init` now divides by 4 and verifies the readback — do not remove that
  check.
- **The receive chain has a ceiling, it is on the IF and not the LO, and it
  differs ~200 MHz between kits.** Corrected 2026-10-15; this entry used to
  claim an LO ceiling. The 2026-09-24 sweep below could not have shown that:
  with the source fixed, `LO = SignalFreq + Rx_freq` moves the LO and the IF
  together, so every row is ambiguous between them.

  Separated on kit `phaser-00` by parking the Pluto's `rx_lo` and sweeping
  only the CN0566 LO, which walks the tone across baseband instead of
  retuning the receiver. At `rx_lo` 2.0 GHz the tone tracked in 11 of 11
  rows; at 2.2 GHz, 0 of 11. The ADF4159 received cleanly at 3.14691 GHz and
  nothing at 3.14950 — 2.6 MHz apart, differing only in `rx_lo`.

  Read on the IF axis, the two kits measured so far:

  | kit | IF clean to | IF dead by |
  |---|---|---|
  | `phaser` | ~2.37 | 2.45 |
  | `phaser-00` | 2.18 | 2.20 |

  The readback matched the request on every row, including the dead ones, and
  MUXOUT reads 0 at every LO — including one producing a 43 dB pattern — so it
  is not lock detect on this board. There is nothing to interrogate at
  runtime. The LO itself was unconstrained across 11.708–12.608 GHz, every LO
  a 10.4 GHz source can reach; a higher LO limit may exist and is not claimed.
- **The IF is a per-unit measurement, not a configuration choice.** Superseded
  the previous entry here, which argued 1.9 GHz was right because
  `10.7 + 2.2 = 12.9` exceeded an LO ceiling. That reasoning does not survive
  the correction above: on the IF axis the HB100 frequency does not affect
  reachability at all. The conclusion was accidentally right and the argument
  was wrong.

  `config.py` ships `Rx_freq = 2.2e9`, which kit `phaser` hears and
  `phaser-00` cannot. Since `config.py` is site-owned and never deployed, a
  hand-edit that rescues one kit cannot travel to the next — which is exactly
  what happened. `phaser_characterize_headless.py` measures the window per
  unit into `calibration.json`, which does deploy, and
  `phaser_functions.rx_freq_source` prefers that measurement over any
  constant. ADI's own production test (`phaser_prod_tst.py`) runs an LO of
  12.1 GHz; characterisation on `phaser-00` independently chose 12.098.
- **Three of the four calibration bugs are upstream ADI's**, so they are
  waiting in any kit built from `pyadi-iio/examples/phaser` rather than from
  this repo. Full write-up with measurements, origins and a triage table in
  [`docs/calibration-bugs.md`](../docs/calibration-bugs.md) — read that before
  debugging a deaf array or a flat beam pattern.
- **The three setup scripts are an order, not a list.** Find HB100 →
  Characterize IF → Calibrate. The IF sweep tunes the LO to track the *stored*
  HB100 frequency so the tone stays in the ~19 MHz analysis window; run it
  before the source has been found and it tracks `config.py`'s 10.525 GHz
  nominal against a source the labs allow anywhere in 10.1–10.7 GHz, hears
  nothing at every IF, and reports that the IF is not the problem. Calibration
  refuses to sweep in that state rather than produce that answer.
- **Running `phaser_find_hb100_headless.py` directly on the Pi does NOT update
  the running service.** Only the UI path triggers `_reload_calibration`. Debug
  over ssh and the backend keeps its old frequency.
- **`config.py` on the Pi is site-owned and never deployed over.** It drifts from
  the repo, and differs between kits: `phaser` carries `Rx_gain = 10` against
  a repo default of 30, costing ~10 dB (it was 1, costing ~27); `phaser-00`
  still reads 1. Check it before believing a weak spectrum. The same file is
  why `Rx_freq` could not be fixed centrally — see the IF entry above.
- **A wrong clock breaks apt and HTTPS.** The Pi has no battery-backed RTC and
  restores `fake-hwclock` time on boot; NTP is blocked on some networks. Symptom
  is apt refusing repo metadata as "not valid yet". Fix with `date -u -s` then
  `fake-hwclock save`, or it reverts on reboot.
- **Tailscale SSH is not key-based.** With `--ssh`, port 22 on the tailnet
  address is answered by tailscaled and authenticated by tailnet identity, so a
  non-tailnet client (a container NATing through the host) cannot connect even
  with an authorized key. HTTP over the tailnet is unaffected.
- **pyadi-iio raises `AttributeError`, not `ImportError`, when libiio is missing
  or ABI-mismatched.** `pytest.importorskip` does not catch it, and one unusable
  module takes down collection for the whole suite.
- **Calibration values are three different units.** `channel_cal` is dB (added to
  `rx_hardwaregain`), `gain_cal` is a linear 0–1 ratio, `phase_cal` is degrees.
- **The receiver runs near full scale and clips the mainlobe flat.** At -40 deg
  the best eight sweep points spanned 8.3 deg inside 0.7 dB, two tied exactly, so
  an argmax wanders 4.8 deg p-p on a *stationary* source — most of a sector
  window. Use `peak_angle_centroid`; parabolic interpolation is worse still.
- **With no source lit, the centroid wanders the full -90..+90.** There is no
  mainlobe to weight, so `DEFAULT_SIGNAL_FLOOR_DB` is load-bearing rather than
  defensive: without it a player who walks away is scored into whichever sector
  the noise favoured.
- **CTF tracking is fed by the sweep loop, not the poll.** A service restart
  leaves the sweep stopped, which is where every install lands. The backend
  reports `measuring:false` and nulls the live readouts rather than serving the
  last angle — anything reading `ctf_status` must honour that flag or it draws
  stale data as live.
- **`install.sh`'s `BACKEND_FILES` is an allowlist.** A backend module missing
  from it installs silently absent and the service crash-loops on the import —
  which is why installing a branch needs *that branch's* `install.sh`.
- **The backend cannot power the Pi off through logind.** It runs unprivileged
  with no active session, so polkit refuses `org.freedesktop.login1.power-off`
  with "authorization requires authentication" — a permission error that looks
  nothing like one. The GUI path works only because of the sudoers drop-in;
  without it, `sudo -n` fails and `shutdown_available` is false. `install.sh`
  installs that drop-in on every run, so a kit that cannot power off is a kit
  installed from a ref older than 2026-09-10, or one where the file was
  deleted.

# Decisions
- **2026-10-15** — Phase calibration fits the whole response curve,
`phi0 = angle(sum(P_linear * exp(j*phi)))`, instead of taking an argmax.
Reason: the argmax was ours, not ADI's — upstream finds the null (`min(gain)`,
+180) and `c032f89` reimplemented it with a peak search after `b66125a`
deleted the originals. Two elements summing give a quadratic peak, so 20
degrees costs 0.26 dB and noise walks the answer tens of degrees, compounding
along the array; five runs on a healthy bench spanned 104 degrees on element
8. Measured 27.5 -> 2.00 degrees of scatter, and 41x on pair 1->2 comparing
both estimators on the same captures. Not a restoration of ADI's null, because
upstream undermines its own: `phase_cal_sweep` computes a peak-bin restriction
and then overwrites it with a whole-spectrum max on the next line, discarding
the protection its own comment says is needed deep in a null. The fit needs
neither a resolved null nor bin tracking.
- **2026-10-15** — Characterisation is a **GUI button, not part of
`provision.sh`**. Reason: provisioning runs unattended — `scripts/pi/firstrun.sh`
passes `--yes`, and `build_kit_image.py --autoprovision` does the same — often
before the kit is assembled and with no HB100 in front of it, which is the one
moment the measurement cannot be made. A prompt there would hang an unattended
run or be skipped by `--yes`, and skipping it is the original failure.
`install.sh` prints the three steps instead when it finds no measurement.
- **2026-10-15** — Calibration will characterise an unmeasured kit and retry,
**once**. Reason: the bench conditions a sweep needs are exactly the ones
calibration needs, so anyone who pressed Calibrate has already set them up.
Bounded by three refusals, which are the substance rather than caveats: a kit
that already has a measurement is never re-swept (a failed gate there means a
dead source or cable, and sweeping would overwrite good data with bad); a
sweep that hears nothing saves nothing; and a kit whose HB100 frequency has
never been measured is not swept at all, since there is nothing to track the
LO against. `should_characterise()` is a named predicate so the policy is
stated once and tested directly.
- **2026-10-15** — The usable IF is **measured per unit** and stored in `calibration.json`, and the measurement beats `config.py`. `phaser_characterize_headless.py` sweeps the IF against the known HB100 and picks the middle of the clean run; `phaser_functions.rx_freq_source` resolves measurement > `Rx_freq_override` > `config.Rx_freq` > bootstrap default, and replaces three defaults that disagreed (`2.2e9`, `2.4e9`, and a hardcoded `2.0e9` in the HB100 search). Reason: kit phaser-00 was deaf at the shipped `config.Rx_freq = 2.2 GHz` while kit "phaser" heard fine there — the usable IF differs by ~200 MHz between units, so any constant is wrong somewhere. `config.py` is site-owned and never deployed, so the hand-edit that kept one kit working could not travel; `calibration.json` does. The middle of the clean run rather than a back-off from the top, because the top edge is the thing that varies. Accepted tradeoff: characterisation is a bench step needing a powered HB100, so it cannot run unattended at provision time.
- **2026-10-15** — The reception limit is on the **IF axis, not the LO**. Measured by parking the Pluto's `rx_lo` and sweeping only the CN0566 LO: at `rx_lo` 2.0 GHz the tone tracked in 11/11 rows, at 2.2 GHz in 0/11, with the ADF4159 clean at 3.14691 GHz and dead at 3.14950. Reason for recording it: `LO = SignalFreq + Rx_freq` with a fixed source moves both together, so the 2026-09-24 sweep could not distinguish them and recorded an LO ceiling that was an IF ceiling. The LO was unconstrained across 11.708–12.608 GHz, every LO a 10.4 GHz source can reach — a higher LO limit may exist and is not claimed either way.
- **2026-09-24** — `install.sh` prints the RF chain it will run (HB100, `Rx_freq`, LO, ADF4159 register) and warns when the LO is past what was measured receiving, or when the IF cannot reach the whole 10.1–10.7 GHz HB100 range. Reason: `config.py` is site-owned and never deployed over, so the numbers that decide the LO are the ones nobody reviews, and a bad LO is undetectable at runtime — the write is accepted, the readback agrees, and MUXOUT is not lock detect here. It warns rather than fails: the ceiling is one kit's measurement, and refusing an install over it would turn a survivable warning into a dead workshop. Thresholds lived in `phaser_functions.lo_warnings`, overridable per kit. **Superseded 2026-10-15** — that sweep moved the LO and the IF together and the limit is on the IF, not the LO; see the entry below.
- **2026-09-10** — GUI shutdown is installed **unconditionally** by `install.sh`; the `PHASER_ALLOW_GUI_SHUTDOWN` gate is gone. Reason: it is a standard feature of the kit, and the flag was never documented in the README, so in practice it only meant shutdown silently did not work on a fresh install. Accepted tradeoff: the backend is unauthenticated, so any reachable kit can be powered off by anyone. Revoke per kit by deleting `/etc/sudoers.d/phaser-shutdown` — the next `install.sh` writes it back.
- **2026-09-04** — The UI arms the gesture only when `get_state` reports `shutdown_available`, probed with `sudo -l` (which answers permission without running anything). Reason: an affordance for something that can only return an error is worse than no affordance.
- **2026-09-04** — Shutdown is a 2 s hold and red, against the CTF control's 1.2 s. Reason: this one cannot be undone from the browser — the machine it stops is the one serving the page.
- **2026-09-04** — CTF scores the **tracked** source by default: the sweep's measured peak, not a commanded beam. Reason: the table challenge is carrying an HB100 in front of the array; `commanded` stays behind `PHASER_CTF_SOURCE` as the fallback, and exactly one source scores at a time.
- **2026-09-04** — Peak angle is a -3 dB power-weighted centroid, not an argmax. Reason: 0.53 deg p-p versus 4.80 deg for argmax on a stationary source — see Traps.
- **2026-09-04** — Tracked confirmation counts 3 consecutive in-sector sweeps, not seconds. Reason: the sweep runs at ~0.9/s, so a 2 s dwell was ~2 observations; `status()` no longer advances the tracked machine, so polling faster cannot confirm sooner.
- **2026-09-04** — `status()` takes `sweeping` and reports `measuring`, nulling the live readouts and dropping the in-flight sweep count when nothing observes. Reason: a stopped sweep looks on screen exactly like a challenge that refuses to score. The trail is kept — earned progress is still earned.
- **2026-09-04** — The start gesture is a 1.2 s hold on the stat-row pill; the sidebar Start button was removed. Reason: `ctf_reset` discards a run and a tap did it silently — three taps in thirty seconds during testing threw away a scored sector.
- **2026-08-27** — Installation runs **on the Pi**: `ssh` in, then `curl -fsSL .../install.sh | bash`. Reason: every deployment bug was client-side (cmd.exe globbing, PATHEXT, no ControlMaster on Windows, `ssh -t` vs sudo, a Store alias posing as `python`); the Pi is the one environment we control, and sudo works normally there.

Older and superseded decisions: see `project/phaser_ARCHIVE.md`.

# Plan

**Phase 1 (current) — Digital Beamforming UI polish:**
1. Wire per-element phase delays — plumb `state.phaseList` (8 zeros default) through `set_state` and apply inside `do_sweep` where `phaseList = [0.0] * 8` currently sits just before `ADAR_set_Phase`. Rename "Set All Phase to 0" button to just "Reset".
2. Mode toggle (Manual / MVDR) inside the Digital Beam Forming sidebar section — radio-style. Manual = current sliders; MVDR = adaptive weights. Show only relevant controls per mode.
3. "2-Element Array Preset" button in Digital Beam Forming — one-click applies `[0, 0, 0, 127, 127, 0, 0, 0]` taper via the existing `set_taper` command. Always visible regardless of mode.

**Phase 2 — MVDR adaptive beamforming (backend):**
- Runs in Python on the Pi. K snapshots of `[chan0, chan1]` IQ → `R̂ = (1/K) Σ x·xᴴ` → for each θ, `s(θ) = [1, exp(j·2π·d·sin(θ)/λ)]ᵀ`, `w_mvdr = R̂⁻¹s / (sᴴR̂⁻¹s)`, `y(θ) = w_mvdrᴴ X`.
- Configurable params: K (snapshots, default 128), diagonal load (default 1e-3).
- References: `docs/2025_Phaser_labs_Python.pdf` "Intro to Adaptive Beamforming"; pysdr.org/content/doa.html#mvdr-capon-beamformer.

**Later:**
- Audit Lab 1–9 presets in `frontend/src/main.js` and backend `get_lab_preset` against `docs/2025_Phaser_labs_Python.pdf` (2025 edition, tracked in the repo). Likely some are stale.
- Plot-range configurability refinement — waiting on user clarification.
- Handle SDR/iiod (port 50901) connection failures gracefully — retry with backoff, SSH-restart iiod option, UI dialog with Retry / Simulation Mode, or pre-check connectivity before init.
- Radar Phase 1 (CW Doppler waterfall) and Phase 2 (FMCW range-Doppler) — separate stream; keep hooks in backend mode dispatcher without pretending range axis exists yet.

# Status
`main` is at `6f31260`, clean and in sync with origin — 240 passing, 4
skipped, workflows green.

**Calibration overhaul shipped 2026-10-15** (#29, #30, #32, #33, #34, #35).
Four silent failures, three of them inherited from upstream ADI
`examples/phaser`: a per-unit IF ceiling that the shipped `Rx_freq = 2.2e9`
exceeds on some kits, four conflicting IFs across five upstream files, a stale
RX buffer plus a hardcoded sample rate in the HB100 search, and phase
calibration's argmax (ours). New `phaser_characterize_headless.py` measures
the IF window per kit into `calibration.json`; a **Characterize IF** button
sits between Find HB100 and Calibrate, which are now in workflow order; the
sidebar shows whether the IF in use was measured on this kit. Measured on
`phaser-00`: array contrast 0.93 -> 32.68 dB, phase scatter 27.5 -> 2.00
degrees. Written up in [`docs/calibration-bugs.md`](../docs/calibration-bugs.md).

**Two kits now, and the note's older entries describe the first one.**

| | `phaser` | `phaser-00` |
|---|---|---|
| reach | LAN `192.168.86.61`, Tailscale `100.81.68.73` | link-local `169.254.210.227` |
| HB100 | 10.4245 GHz | 10.40769 GHz |
| IF | 1.9 GHz, hand-edited into `config.py` | **1.690 GHz, measured** and stored |
| IF ceiling | ~2.37 GHz | 2.18 GHz |
| `Rx_gain` in `config.py` | 10 | 1 |

`phaser-00` carries a calibration taken after its HB100 was bumped, so its
`pcal` may hold a steering term — see ToDo. Its IF window is independent of
source position and does not need redoing.

Two features are shipped and verified on hardware: GRCon26 CTF tracking mode,
and GUI shutdown (a 2 s hold on the connection pill). As of 2026-09-10 shutdown
is granted on every install rather than per machine. Both are covered in
Decisions; the hardware evidence is in the archive.

**CTF knobs, for the table:** the flag and sequence live in
`/etc/default/phaser-ctf` and are in no repo. Thresholds are env-tunable
(`PHASER_CTF_SOURCE`, `_TOLERANCE_DEG`, `_DWELL_S`, `_TRACK_SWEEPS`,
`_SIGNAL_FLOOR_DB`) so they can be loosened without a redeploy.

**Turning GUI shutdown off on one kit:** it is granted on every install now, so
removing it is a per-kit action that has to be repeated after each install:

```bash
sudo rm /etc/sudoers.d/phaser-shutdown
sudo systemctl restart phaser-headless
```

The restart is not optional — `shutdown_permitted()` caches the `sudo -l` probe
for the process lifetime, so without it the UI keeps offering a gesture that now
errors. Afterwards `get_state` reports `shutdown_available: false` and the pill
goes back to being a plain readout.

**Reaching the kits:** see the table above; the Overview's `.20` is stale.
HTTP works over the tailnet from anywhere; Tailscale SSH authenticates by
tailnet identity — see Traps. `phaser-00` is link-local over direct ethernet
and takes a key via `ssh-copy-id`; it has `sudo` but not passwordless, so
stopping the service for bench work is `pkill -TERM -f "phaser_headl[e]ss.py"`
(the bracket stops the pattern matching your own ssh command line).

**Next is beamforming Phase 1, untouched.** The per-element phase sliders still
send `state.phaseList` and the backend still ignores it.

Branches: `main` only, locally. `origin` carries `main`, a stale `radar-dev`
(an ancestor of `main`), and seven abandoned `claude/*` refs.

**Kit provisioning** merged in #24 (`scripts/provision.sh`, `scripts/pi/`,
`tools/prep_sdcard.py`, `tools/build_kit_image.py`, `docs/golden-image.md`).
Still unproven on hardware end to end — next opportunity is the new-kit
bring-up in ToDo, which is the first run that exercises it for real.

Five unrelated PRs remain open: #31 (tripod stand), #28 (pin `pylibiio<1`),
#26 (hostname from the card), #25 (Signal Freq never retuning the LO,
CONFLICTING), #19 (CTF env docs).

# ToDo
- [ ] **Re-run Calibrate on `phaser-00` with the HB100 re-aimed at mechanical boresight.** Its HB100 was bumped mid-session (search SNR 75.1 -> 66.4 dB) and phase cal assumes boresight, so the stored `pcal` may carry a steering term. The IF window does NOT need redoing
- [ ] **Bring up a new kit end to end** — `provision.sh`, then Find HB100 -> Characterize IF -> Calibrate. First real hardware exercise of the provisioning work from #24. Record this kit's IF ceiling: `phaser` is ~2.37 GHz and `phaser-00` 2.18, and a third point is worth more than further reasoning about whether per-unit measurement was the right call
- [ ] **Check the 4<->5 element phase step after the boresight re-run.** `phaser-00` measures ~142 deg there. That is the Pluto channel split, which ADI's production test gives extra tolerance for (`phase_cal_limits = [90, 90, 90, 120, 90, 90, 90]`), but 142 exceeds their own 120 limit
- [ ] **Decide whether the IF edge is the AD9361 or the CN0566's IF path.** Untested, and it does not change the fix — both are per-kit — but it says whether swapping a Pluto moves the window
- [ ] **Set `Rx_gain = 30` in the Pi's `config.py`** — deliberately not changed for you. It now reads **10** (was 1), so the sweep is ~10 dB down rather than ~27
- [ ] Wire per-element phase delays into `do_sweep` (Plan Phase 1 item 1)
- [ ] Rename "Set All Phase to 0" → "Reset"
- [ ] Add Manual / MVDR mode toggle in Digital Beam Forming
- [ ] Add "2-Element Array Preset" button (`[0, 0, 0, 127, 127, 0, 0, 0]` taper)
- [ ] Implement MVDR backend (Plan Phase 2)
- [x] Make `find_hb100` **refuse to save** on a bad result — its range and SNR checks are warnings only, so with no source present it wrote a bogus calibration twice. *Done in #18: nothing is saved unless the spur-rejected SNR and a live LO-shift confirmation both pass.*
- [x] Gate `channel_calibration` on signal presence — it returned a 348 dB correction against noise, which is unusable by construction (`Rx_gain + ccal` far outside the driver's range). *Done in #18: `phaser_cal_headless.py` aborts before any calibration step when all-on vs all-off array contrast is under 6 dB.*
- [ ] Release the iio contexts on mode change — nothing ever closes them; four sockets stay open regardless of mode. Prerequisite for the sim toggle below, and it also fixes calibration's broken-pipe-on-first-attempt. *Still reproducing 2026-10-15: `CN0566.SDR_init` writes the AD9361 `initialize` debug attr, which resets the part and drops the context, so the first run after the service exits loses the race. Every headless entrypoint now retries it, which hides the symptom rather than fixing it.*
- [ ] Build sim start into the GUI as a live toggle so there is one way to launch (`--sim` becomes the initial value only). Needs the teardown above; capabilities differ by source (CW radar refuses in sim, interferer control is sim-only)
- [ ] Add the Windows + Linux CI matrix — deferred; without it the Windows half of the test suite never runs, and the golden-tar test is meaningless as a single-platform check
- [ ] Audit Lab 1–9 presets against `docs/2025_Phaser_labs_Python.pdf`
- [ ] Consider feeding the three upstream bugs back to `analogdevicesinc/pyadi-iio` — the single `rx()` and hardcoded sample rate in `phaser_find_hb100.py`, `phase_cal_sweep` overwriting its own peak-bin restriction, and the four conflicting IFs. All are documented with measurements in `docs/calibration-bugs.md`
- [ ] Handle iiod / SDR connection failures gracefully (retry, restart, UI fallback)
- [ ] Clarify plot-range configurability request
- [ ] Provision one stock Kuiper card end to end with `provision.sh`, and boot one `build_kit_image.py --autoprovision` image, before relying on either at a workshop
