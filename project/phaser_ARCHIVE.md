---
name: "#phasergui archive"
dateCreated: 2026-08-27
dateModified: 2026-09-04
---
Cold storage for `phaser_PROJECT.md`. Nothing here describes the present.

# Session Log

## 2026-08-25 — Infrastructure hardening (closed, verified)

Deploy path `python deploy.py [host]`; first-time provisioning via
`scripts/setup.sh` / `setup.ps1`, anchored to the repo root, no Node required.
CI green on `main`, no deprecation annotations, reporting "dist/ unchanged" --
confirming the EOL churn was fixed rather than quieter. Verified in the fresh
WSL clone: only the three `.ftr` files contain CR; `deploy.py --sim-only`
succeeds with no Node; `uv sync` matched `uv.lock` with no drift; sim mode
served `:8080` (page + vendored 3.5 MB Plotly) and radar `:8081`; CW Doppler
math passed all five synthetic cases, worst error 0.05 m/s against a 0.1125 m/s
bin.

Superseded 2026-08-27: `setup.sh`/`setup.ps1` are gone, replaced by `install.sh`
running on the Pi.

## 2026-08-26 — New-Pi deploy blocked (closed, fixed)

A new Pi took every file, printed "Deployment complete!", and had no
`phaser-headless.service`. `deploy.py` was the update path and never installed
the unit; only `setup-pi.sh` did. Step 4's restart ran with `check=False`, so a
missing unit degraded to a `WARN:` and the script still exited 0.

Also found and fixed in the same stretch: `config.py` never seeded (fresh Pi
crash-looped on `import config`); the three runtime pip packages installed only
by `setup-pi.sh`; `scp` could not create the install dir; `setup.sh` committed
mode 100644 so `./scripts/setup.sh` failed with Permission denied; `setup-pi.sh`
needed root sudo that could never work under its own piped-stdin invocation;
both wrappers built the radar frontend then called `deploy.py` without `--radar`.

Diagnosis was initially read off a **stale** OneDrive copy at `b505f4f` because
`cdocker.yaml` still mounted the dead path. Resolved -- the container now mounts
`~/projects/phaser`.

All closed by PRs #2 and #3.

## 2026-09-04 — CTF tracking mode (closed, merged)

Rebuilt the GRCon26 CTF around the tracked source and merged it as PR #1
(`main` at `6365ede`). Backend in `phaser_ctf.py` + the `do_sweep` hook;
panel rebuilt around the stat-row pill.

Estimator comparison, measured live at -40 deg with the HB100 lit and the
horn stationary (27 frames):

| estimator | std | p-p | max dev |
|---|---|---|---|
| argmax | 1.03 | 4.80 | 2.52 |
| centroid (-3 dB) | 0.15 | 0.53 | 0.34 |
| parabolic | 0.88 | 4.43 | 2.53 |

At boresight the same comparison gave argmax 0.61 / 1.84 / 0.92 and centroid
0.20 / 0.90 / 0.67. The clipped mainlobe spanned 8.3 deg inside 0.7 dB at
-40 deg and 6.4 deg at boresight, consistent with 1/cos(theta) broadening
(13.0 deg at boresight, 17.0 deg at 40 deg, for d/lambda = 0.4868).

Signal floor, measured both ways: HB100 lit, peak within ~1 dB of full scale;
powered off, -50.7 dB mean, -49.3 dB worst, std 0.45, sweep minimum -54.6.
The two states are ~50 dB apart, so the -30 dB floor sits ~19 dB clear of the
dark peak. With the source dark the centroid ranged the full -90..+90.

Hand-carried transition (sector 2 -> dead band -> sector 3, 40 frames at
0.89 sweeps/s): 8.1 s in sector 2, 11.5 s crossing the dead band, 23.0 s in
sector 3, with exactly 2 sector changes and no spurious flips. Boundaries were
crisp at the geometric edge, single-sample, so no hysteresis was needed. Hand
drift during the 23 s hold was 3.6 deg — the source jitter is 0.3 deg, the
human is 3.6, which is why the +/-5 deg tolerance was not tightened.

Sweep grid is not a limiting factor: `ignore_res=True` phase-steps the sweep,
giving 9-11 grid points inside every +/-5 deg window (local step 0.92-1.28 deg).

Two runs of the real configured sequence completed and issued the flag, the
second entirely through the new UI.

Debugging note worth not repeating: an hour went into "the CTF numbers never
lock in" before the cause was found, and it was never the sectors. The backend
had received no `set_state` at all that browser session, and the panel showed
no angle or sector, so there was nothing on screen to debug from. `ctf_status`
already carried `current_angle_deg`, `current_sector` and `holding`; the panel
threw all three away.

## 2026-09-04 — Deployment, calibration and live sweep (rotated from Status)

Install is two lines from any OS — `ssh analog@phaser.local` then
`curl -fsSL .../install.sh | bash`. It runs on the Pi, is idempotent, updates a
drifted systemd unit, swaps `dist/` atomically, never overwrites `config.py`,
and verifies the service came up. Offline variants documented (`PHASER_SRC` for
the tree, `PHASER_WHEELS` for the pip packages). `setup.sh`/`setup.ps1` are gone.
`deploy.py` remains for the edit/test loop, rebuilt on a testable `ssh_argv`
seam.

Calibration was fixed and exercised on hardware. `spec_est` and the three array
calibration routines were restored — `b66125a` had stripped them while leaving
their imports in place, which is why Find HB100 "hung" (an `ImportError` in
~50 ms, surfaced as a hang by a modal that read a `data` envelope the backend
never sends). Storage is now one `calibration.json`.

Live sweep, two stacked faults: `SDR_LO_init` wrote the mixer LO into the
ADF4159 without the `/4` the CN0566 needs, and the Pi's `config.py` carried
`Rx_gain = 1`. Peak prominence over the median measured on hardware: 10.6 dB
(as shipped) -> 18.4 dB (LO fixed) -> 45.3 dB (LO fixed + gain 30), against
53.6 dB from `find_hb100`. The LO half is merged; the gain half is site config.

# Ruled Out

- **Device contention as the cause of bad calibration values.** The backend
  holds four iio sockets open continuously regardless of mode, but the main loop
  does not call `rx()` while idle, and `run_calibration` sets idle before
  spawning. Contention affects connection setup only (a broken pipe on attempt
  1, absorbed by the retry), not sample integrity. Not worth re-architecting.
- **`Rx_gain` alone as the cause of the empty spectrum.** It contributes ~27 dB
  but the LO `/4` was the primary fault; gain alone still left only 18 dB
  prominence.
- **"The browser UI never showed signal."** Asserted from a code read on
  2026-08-27 and wrong -- it worked at a conference in July. With `Rx_gain=30`
  and an LO left correctly programmed by a preceding calibration run, the path
  works.

# Rotated Decisions (still true, just closed)

_Rotated 2026-09-04 from the hot note to meet its size budget. Still true._

- **2026-08-25** — Project moved off OneDrive to `~/projects/phaser` in WSL, as a fresh clone. Reason: OneDrive forced venvs out-of-tree and made recursive greps time out. Venv in-tree (`.venv/`, uv, Python 3.12), `.python-version` tracked.
- **2026-08-25** — `browser-based` renamed to `main`; `radar-dev` split off, CI building `dist/` on both. Reason: the two feature streams were entangled on one branch.
- **2026-08-25** — `frontend/dist/` committed and built by CI; `deploy.py` does not build by default. Reason: a machine with only Python and ssh can deploy a working UI.
- **2026-08-25** — The built UI is fully offline: Plotly pinned to 2.30.0 and vendored at a stable unhashed filename, fonts committed, CI failing on any external reference. Reason: workshop networks are frequently isolated.
- **2026-08-25** — The three `LTE*.ftr` filter configs are tracked and deployed. Reason: pyadi-iio resolves them against the process CWD, and the load is wrapped in a `try`/`except` that only warns — a missing file degrades *silently* to an unfiltered wideband search.
- **2026-08-27** — `scripts/setup.sh` and `setup.ps1` deleted. Reason: two implementations of the same logic in two languages had already diverged three ways; `install.sh` replaces both from any OS.
- **2026-08-27** — Repo made public. Reason: makes the install a genuine two-line paste with no token; the tracked lab PDF is already public on the ADI site.

- **2026-08-25** — `.gitattributes` normalizes to LF (`* text=auto eol=lf`), with `*.ftr -text`. Reason: CRLF in committed `dist/` caused ~781 lines of EOL churn per push; `.ftr` files are vendor CRLF and must keep their bytes.
- **2026-08-25** — Backend entrypoints, helper modules and `.ftr` files stay in the repo root. Reason: they are placed flat in the Pi's working directory and resolve each other by bare import / bare filename.
- **2026-06-25** — Digital beamforming Phase 1 (manual weights) ships before MVDR. Reason: manual weights are contained; MVDR needs backend covariance estimation and workshop-verified math.
- **~2026-06-23** — Radar build-out is CW-first, FMCW second. Reason: CW needs no TDD or ramp config and lifts existing code from `phaser_service.py`.

# Superseded Decisions

- **2026-08-25** — `deploy.py` is the deploy path, `scripts/setup.sh` the
  first-time provisioning path. Superseded 2026-08-27: `install.sh` on the Pi is
  the install path; both wrappers deleted.
- **2026-06-25** — Freeze button placement and plot `autorange: false`; shipped,
  no forward consequence.
- **~2026-05-22** — Tauri build path is platform-specific, Rust needed in WSL.
  No longer a live path.
