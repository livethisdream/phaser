---
name: "#phasergui"
dateCreated: 2026-08-06
dateModified: 2026-10-31
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
- **The receive chain's ceiling is on the IF, not the LO, and it differs
  ~200 MHz between kits.** Corrected 2026-10-15; this used to claim an LO
  ceiling, because the sweep behind it moved the LO and the IF together and
  could not tell them apart. Measured clean to IF 2.37 GHz on kit `phaser`
  and 2.18 on `phaser-00`. The LO was unconstrained everywhere a 10.4 GHz
  source can reach it. Writes are accepted and read back at any setting and
  MUXOUT is not lock detect here, so nothing is interrogable at runtime.
- **The IF is a per-unit measurement, not a configuration choice.** `config.py`
  ships `Rx_freq = 2.2e9`, which `phaser` hears and `phaser-00` cannot, and
  `config.py` is never deployed — so the hand-edit that rescues one kit cannot
  travel. `phaser_characterize_headless.py` measures it into
  `calibration.json`, which does deploy, and `rx_freq_source` prefers that
  over any constant.
- **Three of the four calibration bugs are upstream ADI's**, so they recur on
  any kit built from `pyadi-iio/examples/phaser`, not just from this repo.
- **The three setup scripts are an order, not a list.** Find HB100 →
  Characterize IF → Calibrate. The IF sweep tracks the *stored* HB100
  frequency; run before the source is found it hears nothing at every IF and
  reports that the IF is not the problem. Calibration refuses in that state.
  Measurements, origins and a triage table for all four bugs:
  [`docs/calibration-bugs.md`](../docs/calibration-bugs.md).
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
  the noise favored.
- **CTF tracking is fed by the sweep loop, not the poll.** A service restart
  leaves the sweep stopped, which is where every install lands. The backend
  reports `measuring:false` and nulls the live readouts rather than serving the
  last angle — anything reading `ctf_status` must honor that flag or it draws
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
- **2026-10-15** — The usable IF is **measured per unit** into `calibration.json` and beats `config.py`; `rx_freq_source` resolves measurement > override > config > default. Reason: kits differ ~200 MHz, `config.py` never deploys, so no constant and no hand-edit can be right everywhere.
- **2026-10-15** — The reception limit is on the **IF axis, not the LO**. Reason: the 2026-09-24 sweep moved both together and could not distinguish them; parking `rx_lo` and sweeping only the LO separated them. Evidence in `docs/calibration-bugs.md`.
- **2026-10-15** — Phase calibration **fits the whole curve** (`angle(sum(P_linear·exp(jφ)))`) rather than taking an argmax. Reason: a quadratic peak makes argmax noise-dominated and it compounds along the array; 27.5 → 2.00 degrees of scatter.
- **2026-10-15** — Characterization is a **GUI button, not part of `provision.sh`**. Reason: provisioning runs unattended at first boot, often before the kit is assembled — the one moment the measurement cannot be made.
- **2026-10-15** — Calibration **characterizes an unmeasured kit and retries, once**. Reason: a sweep needs exactly the bench conditions calibration needs. Refuses if the kit is already measured, if the HB100 frequency is not, or if the sweep hears nothing.
# Plan

**Phase 1 (current) — Digital Beamforming UI polish:**
1. Wire per-element phase delays — plumb `state.phaseList` (8 zeros default) through `set_state` and apply inside `do_sweep` where `phaseList = [0.0] * 8` currently sits just before `ADAR_set_Phase`. Rename "Set All Phase to 0" button to just "Reset".
2. Mode toggle (Manual / MVDR) inside the Digital Beam Forming sidebar section — radio-style. Manual = current sliders; MVDR = adaptive weights. Show only relevant controls per mode.
3. "2-Element Array Preset" button in Digital Beam Forming — one-click applies `[0, 0, 0, 127, 127, 0, 0, 0]` taper via the existing `set_taper` command. Always visible regardless of mode.

**Phase 2 — MVDR adaptive beamforming (backend):**
- Runs in Python on the Pi. K snapshots of `[chan0, chan1]` IQ → `R̂ = (1/K) Σ x·xᴴ` → for each θ, `s(θ) = [1, exp(j·2π·d·sin(θ)/λ)]ᵀ`, `w_mvdr = R̂⁻¹s / (sᴴR̂⁻¹s)`, `y(θ) = w_mvdrᴴ X`.
- Configurable params: K (snapshots, default 128), diagonal load (default 1e-3).
- References: `docs/2025_Phaser_labs_Python.pdf` "Intro to Adaptive Beamforming"; pysdr.org/content/doa.html#mvdr-capon-beamformer.

**Later:** lab-preset audit, plot-range configurability, and graceful
iiod-failure handling are carried in ToDo rather than restated here. Radar
(CW Doppler waterfall, then FMCW range-Doppler) is a separate stream on
`radar-dev`; keep the backend mode dispatcher's hooks without pretending a
range axis exists yet.

# Status
`main` is at `6f31260`, clean and in sync with origin — 240 passing, 4
skipped, workflows green.

**Calibration overhaul shipped 2026-10-15** (#29, #30, #32–#36). Four silent
failures, three inherited from upstream ADI `examples/phaser`. New
`phaser_characterize_headless.py` measures each kit's usable IF into
`calibration.json`; a **Characterize IF** button sits between Find HB100 and
Calibrate, now in workflow order; the sidebar shows whether the IF in use was
measured on this kit; calibration self-recovers once on an unmeasured one.
On `phaser-00`: array contrast 0.93 → 32.68 dB, phase scatter 27.5 → 2.00
degrees. Detail in [`docs/calibration-bugs.md`](../docs/calibration-bugs.md).

**Two kits now; older entries in this note describe the first only.**

| | `phaser` | `phaser-00` |
|---|---|---|
| reach | LAN `192.168.86.61`, Tailscale `100.81.68.73` | link-local `169.254.210.227` |
| HB100 | 10.4245 GHz | 10.40769 GHz |
| IF | 1.9 GHz, hand-edited into `config.py` | **1.690 GHz, measured** |
| IF ceiling | ~2.37 GHz | 2.18 GHz |
| `Rx_gain` in `config.py` | 10 | 1 |

`phaser-00`'s HB100 was bumped mid-session, so its stored `pcal` may carry a
steering term; its IF window is unaffected. `phaser-00` has `sudo` but not
passwordless — stop the service for bench work with
`pkill -TERM -f "phaser_headl[e]ss.py"` (the bracket stops the pattern
matching your own ssh command line).

**Next is beamforming Phase 1, untouched.** The per-element phase sliders
still send `state.phaseList` and the backend still ignores it.

Kit provisioning (#24) is merged but unproven on hardware end to end; the
new-kit bring-up in ToDo is the first run that exercises it. Five unrelated
PRs remain open: #31, #28, #26, #25 (CONFLICTING), #19.

CTF knobs, the per-kit shutdown opt-out, and the other operational runbooks
moved to `project/phaser_ARCHIVE.md` on 2026-10-15.

# ToDo
- [ ] **Re-run Calibrate on `phaser-00` with the HB100 re-aimed at boresight** — it was bumped mid-session, so the stored `pcal` may carry a steering term. The IF window is unaffected
- [ ] **Bring up a new kit end to end** — first real hardware run of #24's provisioning. Record its IF ceiling; a third point (against 2.37 and 2.18) is worth more than further argument about per-unit measurement
- [ ] **Check the 4↔5 phase step after that re-run** — `phaser-00` reads ~142°, past ADI's own 120° limit for that pair (the Pluto channel split)
- [ ] Establish whether the IF edge is the AD9361 or the CN0566 IF path — does not change the fix, but says whether swapping a Pluto moves the window
- [ ] **Set `Rx_gain = 30` in the Pi's `config.py`** — deliberately not changed for you. It now reads **10** (was 1), so the sweep is ~10 dB down rather than ~27
- [ ] Wire per-element phase delays into `do_sweep` (Plan Phase 1 item 1)
- [ ] Rename "Set All Phase to 0" → "Reset"
- [ ] Add Manual / MVDR mode toggle in Digital Beam Forming
- [ ] Add "2-Element Array Preset" button (`[0, 0, 0, 127, 127, 0, 0, 0]` taper)
- [ ] Implement MVDR backend (Plan Phase 2)
- [ ] Release the iio contexts on mode change — four sockets stay open regardless of mode. Prerequisite for the sim toggle below. *Still reproducing 2026-10-15: `CN0566.SDR_init` writes the AD9361 `initialize` attr, resetting the part and dropping the context; every entrypoint now retries past it, hiding the symptom*
- [ ] Build sim start into the GUI as a live toggle so there is one way to launch (`--sim` becomes the initial value only). Needs the teardown above; capabilities differ by source (CW radar refuses in sim, interferer control is sim-only)
- [ ] Add the Windows + Linux CI matrix — deferred; without it the Windows half of the test suite never runs, and the golden-tar test is meaningless as a single-platform check
- [ ] Audit Lab 1–9 presets against `docs/2025_Phaser_labs_Python.pdf`
- [ ] Consider reporting the three upstream bugs to `analogdevicesinc/pyadi-iio` — all documented with measurements in `docs/calibration-bugs.md`
- [ ] Handle iiod / SDR connection failures gracefully (retry, restart, UI fallback)
- [ ] Clarify plot-range configurability request
- [ ] Provision one stock Kuiper card end to end with `provision.sh`, and boot one `build_kit_image.py --autoprovision` image, before relying on either at a workshop

## Radar (`radar-dev`)
Detail and history in [`docs/fmcw-phase-c-plan.md`](../docs/fmcw-phase-c-plan.md).
- [ ] **C5 bench test of PR #76 on the kit**: 0 m calibration through the pop-up; range at 1, 2 and 3 m; MTI; CFAR in both modes; a clean exit to beamforming; the Hardware pane showing each preset's frequency and gains in FMCW mode
- [ ] Merge #75 (brings `main`'s #70 into `radar-dev`) and #76 once C5 passes
- [ ] **Phase D: browser radar simulator** -- port the radar sim and DSP to JS, a sim transport for `frontend-radar/`, radar parity cases, the radar UI on Pages. Write the phased plan and review it before code (plan doc, *Follow-on*)
- [ ] Pi clock: no RTC and no NTP client, so a late network leaves it wrong (tar warns about future timestamps on install)
- [ ] Merge `radar-dev` into `main` once the radar work is ready
- [ ] Optional, offered 2026-10-30: write the settings-pane rules (explanations in hover tooltips, same-size aligned controls, grey out what does nothing) into CLAUDE.md
