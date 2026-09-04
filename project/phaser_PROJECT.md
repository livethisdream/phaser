---
name: "#phasergui"
dateCreated: 2026-08-06
dateModified: 2026-09-04
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
- **Running `phaser_find_hb100_headless.py` directly on the Pi does NOT update
  the running service.** Only the UI path triggers `_reload_calibration`. Debug
  over ssh and the backend keeps its old frequency.
- **`config.py` on the Pi is site-owned and never deployed over.** It drifts from
  the repo: this Pi carries `Rx_gain = 1` against a repo default of 30, which
  costs ~27 dB. Check it before believing a weak spectrum.
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

# Decisions
- **2026-09-04** — CTF scores the **tracked** source by default: the sweep's measured peak, not a commanded beam. Reason: the table challenge is carrying an HB100 in front of the array; `commanded` stays behind `PHASER_CTF_SOURCE` as the fallback, and exactly one source scores at a time.
- **2026-09-04** — Peak angle is a -3 dB power-weighted centroid, not an argmax. Reason: 0.53 deg p-p versus 4.80 deg for argmax on a stationary source — see Traps.
- **2026-09-04** — Tracked confirmation counts 3 consecutive in-sector sweeps, not seconds. Reason: the sweep runs at ~0.9/s, so a 2 s dwell was ~2 observations; `status()` no longer advances the tracked machine, so polling faster cannot confirm sooner.
- **2026-09-04** — `status()` takes `sweeping` and reports `measuring`, nulling the live readouts and dropping the in-flight sweep count when nothing observes. Reason: a stopped sweep looks on screen exactly like a challenge that refuses to score. The trail is kept — earned progress is still earned.
- **2026-09-04** — The start gesture is a 1.2 s hold on the stat-row pill; the sidebar Start button was removed. Reason: `ctf_reset` discards a run and a tap did it silently — three taps in thirty seconds during testing threw away a scored sector.
- **2026-08-27** — Installation runs **on the Pi**: `ssh` in, then `curl -fsSL .../install.sh | bash`. Reason: every deployment bug was client-side (cmd.exe globbing, PATHEXT, no ControlMaster on Windows, `ssh -t` vs sudo, a Store alias posing as `python`); the Pi is the one environment we control, and sudo works normally there.
- **2026-08-27** — `deploy.py` kept, rebuilt on a pure `ssh_argv` seam with tests. Reason: `install.sh` provisions, `deploy.py` iterates — it ships a working tree to the Pi without a round trip through GitHub.
- **2026-08-27** — Calibration consolidated into one `calibration.json`, read with a **per-key** fallback to the legacy pickles. Reason: finishes a JSON migration the headless rewrite silently reverted, and drops `pickle` from the load path. Per-key so re-running one calibration cannot revert the others to defaults.
- **2026-08-27** — Windows is a supported target but **unverified**. Reason: the Windows box has no real Python, only the Store alias, so that path is reasoned-about rather than executed.

Older and superseded decisions: see `archive/Phaser GUI Update Archive.md`.

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
**CTF tracking mode is merged and running on the array.** `main` is at `a4a765e`
(PR #1 plus follow-ups), 94 tests passing and 1 skipped, all three workflows
green. The Pi runs `main` as of 2026-09-04.

The challenge: the array sweeps while a player carries an HB100 in front of it.
`do_sweep` returns `peak_angle_deg` (a -3 dB power-weighted centroid),
`CtfMode.observe_tracked` maps it to one of five sectors at -40/-20/0/+20/+40 deg
(+/-5 deg each), and three consecutive in-sector sweeps confirm one. Walking the
configured sequence returns the flag. Scoring is backend-side; the flag and
sequence live in `/etc/default/phaser-ctf` and are in no repo. Thresholds are
env-tunable (`PHASER_CTF_SOURCE`, `_TOLERANCE_DEG`, `_DWELL_S`, `_TRACK_SWEEPS`,
`_SIGNAL_FLOOR_DB`) so they can be loosened at the table without a redeploy.

Verified end to end on hardware, including two complete runs of the real sequence
ending in an issued flag, and `measuring` flipping correctly with the sweep
stopped and restarted; evidence is in the archive. The sidebar panel holds no
controls — sector table, bands toggle, and a status line that speaks only when
the run has not started or has completed.

**Reaching the Pi:** LAN `192.168.86.61` (the Overview's `.20` is stale), or
Tailscale `100.81.68.73` / `phaser`. HTTP works over the tailnet from anywhere;
Tailscale SSH authenticates by tailnet identity — see Traps.

HB100 reads 10.4245 GHz. Deployment, calibration and the live sweep are done and
merged; that history is in the archive.

Per-element phase sliders still send `state.phaseList`; the backend still does
not apply them. Beamforming Phase 1 is otherwise untouched.

# ToDo
- [ ] **Set `Rx_gain = 30` in the Pi's `config.py`** — deliberately not changed for you. It now reads **10** (was 1), so the sweep is ~10 dB down rather than ~27
- [ ] Delete the merged `claude/ctf-mode` branch
- [ ] Wire per-element phase delays into `do_sweep` (Plan Phase 1 item 1)
- [ ] Rename "Set All Phase to 0" → "Reset"
- [ ] Add Manual / MVDR mode toggle in Digital Beam Forming
- [ ] Add "2-Element Array Preset" button (`[0, 0, 0, 127, 127, 0, 0, 0]` taper)
- [ ] Implement MVDR backend (Plan Phase 2)
- [ ] Make `find_hb100` **refuse to save** on a bad result — its range and SNR checks are warnings only, so with no source present it wrote a bogus calibration twice
- [ ] Gate `channel_calibration` on signal presence — it returned a 348 dB correction against noise, which is unusable by construction (`Rx_gain + ccal` far outside the driver's range)
- [ ] Release the iio contexts on mode change — nothing ever closes them; four sockets stay open regardless of mode. Prerequisite for the sim toggle below, and it also fixes calibration's broken-pipe-on-first-attempt
- [ ] Build sim start into the GUI as a live toggle so there is one way to launch (`--sim` becomes the initial value only). Needs the teardown above; capabilities differ by source (CW radar refuses in sim, interferer control is sim-only)
- [ ] Collapse the duplicated placement logic — `install.sh` and `deploy.py` each implement it; have `deploy.py` ship the tree and invoke `install.sh` with `PHASER_SRC`
- [ ] Add the Windows + Linux CI matrix — deferred; without it the Windows half of the test suite never runs, and the golden-tar test is meaningless as a single-platform check
- [ ] Audit Lab 1–9 presets against `docs/2025_Phaser_labs_Python.pdf`
- [ ] Handle iiod / SDR connection failures gracefully (retry, restart, UI fallback)
- [ ] Delete the superseded `fix/fresh-pi-deploy-provisioning` branch (fully contained in `main`)
- [ ] Decide the fate of stale local branches; `radar-app` (`667c19c`) is **not** merged
- [ ] Clarify plot-range configurability request
