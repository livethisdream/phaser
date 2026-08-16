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

**Observations come from the commanded `phaseList`, not from the sweep.**
`do_sweep()` walks `SteerValues` across the whole steer range and writes phases
at every step by design; hooking that would see every sector on every sweep.
`self.phaseList` is where the operator deliberately pointed the beam, which is
the thing this challenge is about.

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

DEFAULT_SECTOR_CENTRES_DEG = [-60.0, -30.0, 0.0, 30.0, 60.0]
DEFAULT_TOLERANCE_DEG = 12.0
DEFAULT_DWELL_S = 2.0

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
                 show_progress=True, allow_sim=True):
        self.sector_centres_deg = list(sector_centres_deg or DEFAULT_SECTOR_CENTRES_DEG)
        self.tolerance_deg = float(tolerance_deg)
        self.dwell_s = float(dwell_s)

        self.target = list(target or _env_list("PHASER_CTF_SEQUENCE") or
                           _parse_sequence(_read_sidecar("ctf_sequence.txt")) or
                           DEMO_SEQUENCE)

        self.flag = (flag or os.environ.get("PHASER_CTF_FLAG") or
                     _read_sidecar("ctf_flag.txt") or PLACEHOLDER_FLAG)

        self.show_progress = bool(show_progress)
        self.allow_sim = _env_bool("PHASER_CTF_ALLOW_SIM", allow_sim)

        self.reset()

    # ---------------------------------------------------------------- state

    def reset(self):
        """Clear the trail. The panel's Start button calls this."""
        self._trail = []
        self._pending_sector = None
        self._pending_since = None
        self._current_sector = None
        self._current_angle = None
        self._matched = False

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
        """
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

    def _advance(self, now):
        """Promote a dwelt-in sector onto the trail, then test the sequence."""
        if self._matched or self._pending_sector is None or self._pending_since is None:
            return
        if now - self._pending_since < self.dwell_s:
            return

        # Confirmed. Only append when it differs from the last confirmed entry:
        # continuing to sit in a sector is not a new entry, and re-entering one
        # only counts after actually leaving it.
        if not self._trail or self._trail[-1] != self._pending_sector:
            self._trail.append(self._pending_sector)
            # Only the tail can ever complete the sequence, so a wrong turn
            # costs the player their progress but never locks them out.
            del self._trail[:-len(self.target)]

        if self._trail == self.target:
            self._matched = True

    def _progress(self):
        """How much of the target the tail of the trail currently satisfies."""
        for length in range(min(len(self._trail), len(self.target)), 0, -1):
            if self._trail[-length:] == self.target[:length]:
                return length
        return 0

    # --------------------------------------------------------------- report

    def status(self, now=None, sim_mode=False):
        """Advance the dwell clock and report. This is what the panel polls."""
        self._advance(time.monotonic() if now is None else now)

        flag_withheld = self._matched and sim_mode and not self.allow_sim

        payload = {
            "status": "ok",
            "data": {
                "configured": self.flag != PLACEHOLDER_FLAG,
                "sectors": [
                    {"sector": i + 1, "centre_deg": c, "tolerance_deg": self.tolerance_deg}
                    for i, c in enumerate(self.sector_centres_deg)
                ],
                "sequence_length": len(self.target),
                "dwell_s": self.dwell_s,
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


def _env_bool(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return raw.strip().lower() not in ("0", "false", "no", "off")
