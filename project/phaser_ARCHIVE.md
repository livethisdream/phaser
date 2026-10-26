---
name: "#phasergui archive"
dateCreated: 2026-08-27
dateModified: 2026-09-10
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

Shape of the shipped challenge, rotated from Status: the array sweeps while a
player carries an HB100 in front of it. `do_sweep` returns `peak_angle_deg` (a
-3 dB power-weighted centroid), `CtfMode.observe_tracked` maps it to one of five
sectors at -40/-20/0/+20/+40 deg (+/-5 deg each), and three consecutive
in-sector sweeps confirm one. Walking the configured sequence returns the flag.
Scoring is backend-side. Two complete runs of the real sequence ended in an
issued flag, and `measuring` flipped correctly with the sweep stopped and
restarted. The sidebar panel holds no controls — sector table, bands toggle, and
a status line that speaks only when the run has not started or has completed.

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

## 2026-09-04 — Branch cleanup (closed)

`claude/ctf-mode` and `fix/fresh-pi-deploy-provisioning` deleted, local and
remote. `claude/ctf-mode` was a clean merge — zero commits in `main..` for both
its local and remote tips.

`fix/fresh-pi-deploy-provisioning` was **not** "fully contained in `main`" as
the ToDo claimed. Three commits (`149626b`, `30987eb`, `1d44d67`) were not
ancestors of `main`; it had been *squash*-merged, so the content was there under
different SHAs. Checked file by file before deleting: `test_calibration_math.py`
and `test_calibration_status.py` identical, `phaser_functions.py` and
`phaser_headless.py` both larger on `main` (127 and 260 lines), and the only
branch-side content `main` lacked was superseded — `hb100_cal.txt` reads that
the 2026-08-27 `calibration.json` decision replaced, and pre-refactor
`phaseList` / `HTTPServer` forms. One thing worth re-adding: the branch carried
a comment on `test_payload_imports.py` reading "Kept in step with deploy.py's
BACKEND_FILES and install.sh's BACKEND_FILES", which is exactly the BACKEND_FILES trap in `phaser_PROJECT.md`.

A `fetch --prune` in the same pass dropped three remote-tracking refs that were
already gone from GitHub: `docs/install-offline`, `feat/offline-wheels`,
`fix/lo-divide-by-four`.

## 2026-09-04 — GUI shutdown (closed, verified on hardware)

Shipped as `ba26820` on `main`: a 2 s hold on the connection pill calls
`power_off`, which `Popen`s `sudo -n /usr/bin/systemctl poweroff`. The physical
button is a `gpio-shutdown` overlay on GPIO21 and logind already maps KEY_POWER
to that same command, so the GUI reproduces the button rather than inventing a
shutdown path.

The grant is `PHASER_ALLOW_GUI_SHUTDOWN=1` on `install.sh`, off by default —
see Decisions. Installed on this Pi at 16:47; `get_state` returned
`shutdown_available: true` over `ws://192.168.86.61:8765`, confirming the
`sudo -l` probe.

Live test at 16:52. All four probes went dark together — LAN and tailnet, 8080
and 22 — and port 22 dying alongside 8080 is what separates a poweroff from a
crashed backend. The previous boot's journal attributes it to the daemon, not a
shell or the button:

    16:52:14 sudo[8028]: analog : PWD=/home/analog/pyadi-iio/examples/phaser ;
                         USER=root ; COMMAND=/usr/bin/systemctl poweroff
    16:52:14 sudo[8028]: session opened for user root(uid=0) by (uid=1000)

No `TTY=` field (the install-time sudo lines five minutes earlier all carry
`TTY=pts/0`) and an empty "by" name — a session-less process, exactly the case
polkit refuses and the sudoers drop-in covers. No KEY_POWER or gpio line
anywhere in that boot, so the hardware button was not involved.

Teardown was clean: "Syncing filesystems and block devices", SIGTERM to
remaining processes, "Finished Power-Off", journal stopped. `wtmp` recorded a
`shutdown system down` entry. The next boot logged no journal recovery, no
unclean mount and no orphan inodes, and `phaser-headless.service` came back
`active`.

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

# Operational runbooks (rotated 2026-10-15)

Still accurate; moved out of the hot note's Status, which is for state
rather than procedure.

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

# Completed ToDo items (rotated 2026-10-15)

- [x] Make `find_hb100` **refuse to save** on a bad result — its range and SNR checks are warnings only, so with no source present it wrote a bogus calibration twice. *Done in #18: nothing is saved unless the spur-rejected SNR and a live LO-shift confirmation both pass.*
- [x] Gate `channel_calibration` on signal presence — it returned a 348 dB correction against noise, which is unusable by construction (`Rx_gain + ccal` far outside the driver's range). *Done in #18: `phaser_cal_headless.py` aborts before any calibration step when all-on vs all-off array contrast is under 6 dB.*

# Ruled Out

- **2026-10-15 — "The reception limit is on the LO axis."** Disproven. The
  2026-09-24 sweep that suggested it moved the LO and the IF together, so it
  could not distinguish them. Parking the Pluto's `rx_lo` and sweeping only
  the CN0566 LO gave 11/11 rows tracking the tone at `rx_lo` 2.0 GHz and 0/11
  at 2.2. The limit is on the IF. It cost a day because the HB100 search
  hardcodes 2.0 GHz and so kept working while calibration went deaf, which
  pointed every suspicion at the calibration code.
- **2026-10-15 — "A stale RX buffer explains phase-calibration scatter."**
  Disproven, twice over. PR #29 anticipated it and this session pursued it
  first. Adding `rx_destroy_buffer()` to `_capture_peak_dbfs` left the scatter
  identical — five runs, element 8 spanning 104 degrees, flushed and
  unflushed alike. The cause was the argmax estimator. The flush was kept
  anyway, on the separate grounds that reading the state you just set is
  correct, but it fixes nothing here.

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

Rotated 2026-10-15: CTF tracking and GUI shutdown, both shipped and
verified on hardware.

- **2026-09-10** — GUI shutdown is installed **unconditionally** by `install.sh`; the `PHASER_ALLOW_GUI_SHUTDOWN` gate is gone. Reason: it is a standard feature of the kit, and the flag was never documented in the README, so in practice it only meant shutdown silently did not work on a fresh install. Accepted tradeoff: the backend is unauthenticated, so any reachable kit can be powered off by anyone. Revoke per kit by deleting `/etc/sudoers.d/phaser-shutdown` — the next `install.sh` writes it back.
- **2026-09-04** — The UI arms the gesture only when `get_state` reports `shutdown_available`, probed with `sudo -l` (which answers permission without running anything). Reason: an affordance for something that can only return an error is worse than no affordance.
- **2026-09-04** — Shutdown is a 2 s hold and red, against the CTF control's 1.2 s. Reason: this one cannot be undone from the browser — the machine it stops is the one serving the page.
- **2026-09-04** — CTF scores the **tracked** source by default: the sweep's measured peak, not a commanded beam. Reason: the table challenge is carrying an HB100 in front of the array; `commanded` stays behind `PHASER_CTF_SOURCE` as the fallback, and exactly one source scores at a time.
- **2026-09-04** — Peak angle is a -3 dB power-weighted centroid, not an argmax. Reason: 0.53 deg p-p versus 4.80 deg for argmax on a stationary source — see Traps.
- **2026-09-04** — Tracked confirmation counts 3 consecutive in-sector sweeps, not seconds. Reason: the sweep runs at ~0.9/s, so a 2 s dwell was ~2 observations; `status()` no longer advances the tracked machine, so polling faster cannot confirm sooner.
- **2026-09-04** — `status()` takes `sweeping` and reports `measuring`, nulling the live readouts and dropping the in-flight sweep count when nothing observes. Reason: a stopped sweep looks on screen exactly like a challenge that refuses to score. The trail is kept — earned progress is still earned.
- **2026-09-04** — The start gesture is a 1.2 s hold on the stat-row pill; the sidebar Start button was removed. Reason: `ctf_reset` discards a run and a tap did it silently — three taps in thirty seconds during testing threw away a scored sector.


- **2026-08-27** — Calibration consolidated into one `calibration.json`, read
  with a per-key fallback to the legacy pickles, dropping `pickle` from the load
  path. Per-key so re-running one calibration cannot revert the others.
- **2026-08-27** — Windows is a supported target but unverified: the Windows box
  has no real Python, only the Store alias. Still open as the CI-matrix ToDo.
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

Superseded 2026-10-15 — the sweep behind this could not tell the LO and
the IF apart, and the limit is on the IF. The replacement is the
2026-10-15 pair in the hot note; see `docs/calibration-bugs.md`.

- **2026-09-24** — `install.sh` prints the RF chain it will run (HB100, `Rx_freq`, LO, ADF4159 register) and warns when the LO is past what was measured receiving, or when the IF cannot reach the whole 10.1–10.7 GHz HB100 range. Reason: `config.py` is site-owned and never deployed over, so the numbers that decide the LO are the ones nobody reviews, and a bad LO is undetectable at runtime — the write is accepted, the readback agrees, and MUXOUT is not lock detect here. It warns rather than fails: the ceiling is one kit's measurement, and refusing an install over it would turn a survivable warning into a dead workshop. Thresholds lived in `phaser_functions.lo_warnings`, overridable per kit. **Superseded 2026-10-15** — that sweep moved the LO and the IF together and the limit is on the IF, not the LO; see the entry below.


- **2026-09-04** — GUI shutdown off by default, granted per machine with
  `PHASER_ALLOW_GUI_SHUTDOWN=1`; the default was meant to *be* the access
  control, so a bench Pi could have it while a floor Pi did not. Superseded
  2026-09-10: the flag was undocumented, so the only thing it reliably produced
  was a fresh install where the shutdown gesture silently did nothing.
  `install.sh` now installs the drop-in every run.
- **2026-08-27** — `deploy.py` kept, rebuilt on a pure `ssh_argv` seam with
  tests, so `install.sh` provisions and `deploy.py` iterates. Superseded by
  `319fd56`, which removed `deploy.py` and the laptop-side setup scripts;
  `install.sh` on the Pi is the only path.
- **2026-08-25** — `deploy.py` is the deploy path, `scripts/setup.sh` the
  first-time provisioning path. Superseded 2026-08-27: `install.sh` on the Pi is
  the install path; both wrappers deleted.
- **2026-06-25** — Freeze button placement and plot `autorange: false`; shipped,
  no forward consequence.
- **~2026-05-22** — Tauri build path is platform-specific, Rust needed in WSL.
  No longer a live path.
