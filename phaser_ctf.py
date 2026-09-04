"""CTF mode — watch where the operator points the beam, in sector order.

Built for the GRCon26 signals CTF. A player at the table has to steer the
array through a five-element sector sequence; when they do, the backend hands
back a flag string.

Design notes worth knowing before changing anything here:

**Everything lives backend-side on purpose.** The browser only renders what
this module reports. `frontend/dist` is committed and served as static files
to every client, so a flag string placed there would be readable with View
Source before anyone steered anything.

**The flag and the target sequence are not in this repo.** Both are read from
the environment or from `ctf_flag.txt` / `ctf_sequence.txt` next to the
backend, which are gitignored. The defaults here are a harmless demo
(`1 2 3 4 5`, a placeholder flag) so the module is testable and so nothing
about the real CTF leaks into a repo that is not the CTF's.

**There are two sources, and exactly one scores at a time.** `tracked` (the
default) scores the sweep's measured peak angle: the array holds still and the
player physically moves the HB100 until the mainlobe finds it. `commanded`
scores a steered phase ramp instead, which is the original challenge and now
the fallback if the RF at the table turns out to be unusable. Both machines
stay live code; `PHASER_CTF_SOURCE` picks one. Letting both score at once would
mean a player who touched the steering UI mid-sweep was being fed by two
different observers.

**The commanded source reads `phaseList`, never the sweep's phases.**
`do_sweep()` walks `SteerValues` across the whole steer range and writes phases
at every step by design; hooking those would see every sector on every sweep.
`self.phaseList` is where the operator deliberately pointed the beam.

That warning does not extend to the sweep's *result*. The tracked source is fed
one measured peak angle per sweep, which is a single observation, not a walk
across the array.

**The ramp arrives sign-flipped.** `main.js` builds it as
`-(360 * d * sin(theta) * f / c)` — the negative of the backend's own
`ConvertSteerAngleToPhase` — so the measured per-element step is negated
before conversion (`RAMP_SIGN`). Without that, sectors mirror about boresight:
2 and 4 swap and everything still looks nearly right.

**Matching is on consecutive distinct sector entries, each held for a dwell.**
Not "every sector seen so far": a target sequence can repeat a sector (leaving
and returning is a real move), which a visited-set cannot represent, and a
loose subsequence match would let a slow drag across the array satisfy almost
anything.
"""

import os
import time

# The frontend builds its steering ramp as the negative of
# ConvertSteerAngleToPhase, so undo that before converting back.
RAMP_SIGN = -1.0

# Per-element phase is rounded to whole degrees in the frontend, so a genuine
# ramp still won't fit perfectly. Anything further off than this is treated as
# "not a steered beam" — e.g. someone dragging individual Phase Control
# sliders, where a steering angle is not a meaningful thing to compute.
RAMP_RESIDUAL_TOLERANCE_DEG = 6.0

# Five sectors inside +/-45 deg. The count is fixed at five by a prior
# commitment -- the sequence "3 1 4 1 2" is plaintext in another challenge's
# payload -- so the +/-45 limit is met by tightening the spacing, never by
# dropping a sector.
#
# +/-45 is a physical limit, not a preference. Past it an 8-element array's
# beamwidth broadens as 1/cos(theta) and the mainlobe stops presenting a clear
# peak, so a player cannot see which sector they are in. Every window edge
# therefore stays inside 45: centre 40 with tolerance 5 reaches exactly 45 and
# no further.
#
# Tolerance is generous despite looking tight. It is applied to the COMMANDED
# angle, recovered from the phase ramp with a worst-case round-trip error of
# 0.022 deg across the range -- there is no measurement noise to absorb, so
# +/-5 is purely "did the operator aim within 5 degrees of the centre", which
# a 1-degree steer control makes easy. The 10 deg dead bands between windows
# are the real reason not to widen it: they keep "between sectors" unambiguous.
DEFAULT_SECTOR_CENTRES_DEG = [-40.0, -20.0, 0.0, 20.0, 40.0]
DEFAULT_TOLERANCE_DEG = 5.0
DEFAULT_DWELL_S = 2.0

# Confirmation for the tracked source is counted in SWEEPS, not seconds. The
# sweep loop runs at about 0.9/s on the Pi, so a 2 s wall-clock dwell is only
# two observations and its meaning drifts with sweep rate; consecutive
# in-sector sweeps says the same thing in the units the measurement actually
# arrives in. Measured on the array, a hand trying to hold a horn still gave 21
# consecutive in-sector sweeps with zero sector changes, so three is generous.
DEFAULT_TRACK_SWEEPS = 3

# Below this the peak is noise, not the source, and the angle is reported as
# None rather than as wherever the noise floor happened to peak.
#
# Measured both ways on the array. Lit, the peak sits within ~1 dB of full
# scale. Powered off, it sits at -50.7 dB mean (-49.3 worst, std 0.45) with the
# sweep minimum around -54.6 -- so the two states are ~50 dB apart and -30
# lands near the middle, ~19 dB clear of the dark peak.
#
# The floor is not a nicety. With no source lit the centroid wanders the full
# -90..+90 of the sweep, because there is no mainlobe to weight; without a
# floor a player who walked away would be scored into whichever sector the
# noise last favoured.
DEFAULT_SIGNAL_FLOOR_DB = -30.0

# "tracked" scores the measured peak, "commanded" scores the steered ramp.
DEFAULT_SOURCE = "tracked"

# Deliberately NOT the CTF's real sequence — see the module docstring.
DEMO_SEQUENCE = [1, 2, 3, 4, 5]
PLACEHOLDER_FLAG = "flag{REPLACE_ME}"


def fit_ramp(phase_list):
    """Recover the per-element phase step from a phase list.

    Returns (step_deg, ok). `ok` is False when the phases are not a straight
    ramp, which means no steering angle can be read off them.
    """
    n = len(phase_list)
    if n < 2:
        return 0.0, False

    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(phase_list) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return 0.0, False

    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, phase_list)) / denom
    intercept = mean_y - slope * mean_x
    residual = max(abs(y - (intercept + slope * x)) for x, y in zip(xs, phase_list))
    return slope, residual <= RAMP_RESIDUAL_TOLERANCE_DEG


def peak_angle_centroid(angles, gain, drop_db=3.0):
    """Angle of the mainlobe peak, as a power-weighted centroid.

    Deliberately NOT an argmax. At the table the receiver runs near full scale,
    which clips the top of the mainlobe flat: measured at -40 deg with the
    HB100 lit, the best eight grid points spanned 8.3 deg inside 0.7 dB and the
    top two tied exactly. An argmax over that wandered 4.8 deg peak-to-peak on
    a STATIONARY source -- most of a 10 deg sector window -- while the centroid
    over the same sweeps held 0.53 deg.

    Parabolic interpolation was worse than either (4.43 deg p-p): it fits
    curvature, and a clipped peak has none.

    Lives here rather than in phaser_headless because CI cannot import that
    module -- pyadi-iio binds libiio at import -- and this is the measurement
    the whole tracked challenge is scored on, so it has to be testable.

    `gain` is in dB, so weights convert back to power first. The mainlobe is
    walked outward from the peak only while samples stay within `drop_db`,
    which stops a strong sidelobe from dragging the centroid off the peak.
    Returns None when there is nothing to measure.
    """
    if not angles or not gain or len(angles) != len(gain):
        return None

    imax = max(range(len(gain)), key=lambda k: gain[k])
    gmax = gain[imax]

    lo = imax
    while lo > 0 and gain[lo - 1] >= gmax - drop_db:
        lo -= 1
    hi = imax
    while hi < len(gain) - 1 and gain[hi + 1] >= gmax - drop_db:
        hi += 1

    weights = [10.0 ** (gain[k] / 10.0) for k in range(lo, hi + 1)]
    total = sum(weights)
    if total <= 0:
        return float(angles[imax])
    return float(
        sum(angles[k] * w for k, w in zip(range(lo, hi + 1), weights)) / total
    )


def _env_list(name):
    """Parse a sector sequence from an env var: "3 1 4 1 2" or "3,1,4,1,2"."""
    raw = os.environ.get(name, "").replace(",", " ").split()
    try:
        return [int(v) for v in raw] or None
    except ValueError:
        return None


def _read_sidecar(filename):
    """Read a single-line sidecar file next to this module, if it exists."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip() or None
    except OSError:
        return None


class CtfMode:
    """Sector-sequence state machine.

    Time-driven rather than purely event-driven: `observe()` records where the
    beam is, `status()` advances the dwell clock. The frontend polls status, so
    a player who steers and then holds still still gets their dwell confirmed
    without any further commands being sent.
    """

    def __init__(self, sector_centres_deg=None, tolerance_deg=DEFAULT_TOLERANCE_DEG,
                 dwell_s=DEFAULT_DWELL_S, target=None, flag=None,
                 show_progress=True, allow_sim=True, source=None,
                 track_sweeps=DEFAULT_TRACK_SWEEPS,
                 signal_floor_db=DEFAULT_SIGNAL_FLOOR_DB):
        self.sector_centres_deg = list(sector_centres_deg or DEFAULT_SECTOR_CENTRES_DEG)

        # Every threshold here is env-tunable, because these are the knobs
        # worth turning at the table and the challenge exists to be enjoyable.
        # Loosening one in the moment must not need a redeploy.
        self.tolerance_deg = _env_float("PHASER_CTF_TOLERANCE_DEG", tolerance_deg)
        self.dwell_s = _env_float("PHASER_CTF_DWELL_S", dwell_s)
        self.track_sweeps = max(1, int(_env_float("PHASER_CTF_TRACK_SWEEPS",
                                                  track_sweeps)))
        self.signal_floor_db = _env_float("PHASER_CTF_SIGNAL_FLOOR_DB",
                                          signal_floor_db)

        chosen = (os.environ.get("PHASER_CTF_SOURCE") or source
                  or DEFAULT_SOURCE).strip().lower()
        self.source = chosen if chosen in ("tracked", "commanded") else DEFAULT_SOURCE

        self.target = list(target or _env_list("PHASER_CTF_SEQUENCE") or
                           _parse_sequence(_read_sidecar("ctf_sequence.txt")) or
                           DEMO_SEQUENCE)

        self.flag = (flag or os.environ.get("PHASER_CTF_FLAG") or
                     _read_sidecar("ctf_flag.txt") or PLACEHOLDER_FLAG)

        self.show_progress = bool(show_progress)
        self.allow_sim = _env_bool("PHASER_CTF_ALLOW_SIM", allow_sim)

        self._clear()
        # Deliberately NOT armed on construction -- see reset().
        self._armed = False

    # ---------------------------------------------------------------- state

    def _clear(self):
        self._trail = []
        self._pending_sector = None
        self._pending_since = None     # commanded: when the beam entered
        self._pending_count = 0        # tracked: consecutive in-sector sweeps
        self._current_sector = None
        self._current_angle = None
        self._matched = False

    def reset(self):
        """Clear the trail and ARM the machine. The panel's Start button calls this.

        Arming is what separates "the operator put the beam here" from "this is
        where the beam happened to be". Without it the machine scores the
        position the frontend pushes when it first connects, and any sector
        sitting under the beam at that moment is a free element -- which at a
        busy table means whatever the previous player left the array pointing
        at. Boresight is the common case, but it is not the only one.

        Note this does NOT require the operator to steer somewhere new. The
        dwell clock restarts here, so pressing Start while already inside the
        first sector and holding it counts as entering that sector. Demanding a
        transition would mean a sequence beginning with the resting sector could
        only be started by steering away and coming back, which is worse than
        the bug it fixes.
        """
        self._clear()
        self._armed = True

    def sector_for_angle(self, angle_deg):
        """Which sector an angle falls in, or None if it's between sectors."""
        if angle_deg is None:
            return None
        for index, centre in enumerate(self.sector_centres_deg):
            if abs(angle_deg - centre) <= self.tolerance_deg:
                return index + 1  # sectors are 1-based, left to right
        return None

    def observe(self, phase_list, convert_phase_to_angle, now=None):
        """Record where the beam is now, from a commanded phase list.

        `convert_phase_to_angle` is the host's ConvertPhaseToSteerAngle, passed
        in rather than reimplemented so the physics has exactly one home.

        Silently does nothing unless this is the commanded source, so that
        steering the UI during a tracked run cannot score.
        """
        if self.source != "commanded":
            return

        now = time.monotonic() if now is None else now

        step, ok = fit_ramp(list(phase_list))
        angle = convert_phase_to_angle(RAMP_SIGN * step) if ok else None
        sector = self.sector_for_angle(angle)

        self._current_angle = angle
        self._current_sector = sector

        if sector != self._pending_sector:
            self._pending_sector = sector
            self._pending_since = now if sector is not None else None

        self._advance(now)

    def observe_tracked(self, angle_deg, signal_db=None):
        """Record where the SOURCE is, from a sweep's measured peak angle.

        This is the tracking challenge: the array holds still and sweeps, the
        player carries the HB100, and the sector is wherever the mainlobe peak
        lands. There is no ramp to fit -- `do_sweep` hands over an angle in
        degrees already.

        Nothing here reads the clock. Confirmation counts consecutive
        in-sector sweeps, so a faster poll cannot confirm a sector sooner.
        """
        if self.source != "tracked":
            return

        if (angle_deg is not None and signal_db is not None
                and signal_db < self.signal_floor_db):
            # Too weak to be the source. None keeps a player who has walked
            # out of the beam "between sectors" rather than parking them in
            # whatever sector the noise floor happened to favour.
            angle_deg = None

        sector = self.sector_for_angle(angle_deg)
        self._current_angle = angle_deg
        self._current_sector = sector

        if sector != self._pending_sector:
            self._pending_sector = sector
            self._pending_count = 0
        if sector is not None:
            self._pending_count += 1

        self._advance_tracked()

    def _confirm(self, sector):
        """Promote a confirmed sector onto the trail, then test the sequence.

        Shared by both sources. They disagree about what makes a sector
        confirmed -- elapsed seconds versus consecutive sweeps -- but not about
        what to do once one is.
        """
        if not self._armed or self._matched or sector is None:
            return

        # Only append when it differs from the last confirmed entry:
        # continuing to sit in a sector is not a new entry, and re-entering one
        # only counts after actually leaving it.
        if not self._trail or self._trail[-1] != sector:
            self._trail.append(sector)
            # Only the tail can ever complete the sequence, so a wrong turn
            # costs the player their progress but never locks them out.
            del self._trail[:-len(self.target)]

        if self._trail == self.target:
            self._matched = True

    def _advance(self, now):
        """Commanded source: confirm once the beam has been held for the dwell."""
        if not self._armed:
            return
        if self._matched or self._pending_sector is None or self._pending_since is None:
            return
        if now - self._pending_since < self.dwell_s:
            return
        self._confirm(self._pending_sector)

    def _advance_tracked(self):
        """Tracked source: confirm once the source has been seen in the same
        sector on `track_sweeps` consecutive sweeps."""
        if not self._armed or self._matched:
            return
        if self._pending_sector is None or self._pending_count < self.track_sweeps:
            return
        self._confirm(self._pending_sector)

    def _progress(self):
        """How much of the target the tail of the trail currently satisfies."""
        for length in range(min(len(self._trail), len(self.target)), 0, -1):
            if self._trail[-length:] == self.target[:length]:
                return length
        return 0

    # --------------------------------------------------------------- report

    def status(self, now=None, sim_mode=False, sweeping=True):
        """Advance the dwell clock and report. This is what the panel polls.

        Only the commanded source is clock-driven, and only it is advanced
        here: a player who steers and then holds still sends no further
        commands, so the poll is what confirms the dwell. The tracked source
        advances on sweeps arriving in `observe_tracked` instead.

        `sweeping` is whether the backend is actually measuring. It defaults
        True so that every existing caller keeps its behaviour; only the
        tracked source cares, since the commanded source is fed by set_state
        and not by the sweep loop.
        """
        if self.source == "commanded":
            self._advance(time.monotonic() if now is None else now)

        # With the sweep stopped nothing is observing, so the last reading is
        # not where the source IS -- it is where the source WAS. Reporting it
        # as live is how a stopped sweep reads as a working challenge that
        # simply refuses to score: the plot still shows a pattern and the bands
        # are still drawn, so there is nothing on screen to say otherwise.
        measuring = bool(sweeping) or self.source == "commanded"
        if not measuring:
            self._current_angle = None
            self._current_sector = None
            # A confirmation chain must not span a blind interval -- the source
            # may have been carried anywhere while nothing was watching. The
            # trail is left alone: progress already earned is still earned.
            self._pending_sector = None
            self._pending_count = 0

        flag_withheld = self._matched and sim_mode and not self.allow_sim

        payload = {
            "status": "ok",
            "data": {
                "configured": self.flag != PLACEHOLDER_FLAG,
                "armed": self._armed,
                "sectors": [
                    {"sector": i + 1, "centre_deg": c, "tolerance_deg": self.tolerance_deg}
                    for i, c in enumerate(self.sector_centres_deg)
                ],
                "source": self.source,
                "measuring": measuring,
                "sequence_length": len(self.target),
                "dwell_s": self.dwell_s,
                "dwell_sweeps": self.track_sweeps,
                "holding_sweeps": self._pending_count,
                "current_angle_deg": self._current_angle,
                "current_sector": self._current_sector,
                "holding": self._pending_sector,
                "matched": self._matched,
                "flag_withheld_in_sim": flag_withheld,
            },
        }

        if self.show_progress:
            payload["data"]["progress"] = self._progress()

        if self._matched and not flag_withheld:
            payload["data"]["flag"] = self.flag

        return payload


def _parse_sequence(text):
    if not text:
        return None
    try:
        return [int(v) for v in text.replace(",", " ").split()] or None
    except ValueError:
        return None


def _env_float(name, default):
    """Read a float from the environment, ignoring anything unparseable.

    A typo in a hand-edited /etc/default/phaser-ctf must not take the whole
    challenge down mid-con; falling back to the default is the safe failure.
    """
    raw = os.environ.get(name)
    if raw is None:
        return float(default)
    try:
        return float(raw.strip())
    except ValueError:
        return float(default)


def _env_bool(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return raw.strip().lower() not in ("0", "false", "no", "off")
