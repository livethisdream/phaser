"""Synthetic tests for phaser_ctf.CtfMode — no hardware, no backend needed.

Builds phase ramps exactly the way `frontend/src/main.js` builds them, feeds
them through the same conversion the backend uses, and checks the sector
state machine behaves under the cases that are easy to get wrong:

  - a +30 deg steer reads as the +30 deg sector, not its mirror image
  - a sector only counts once it has been held for the dwell
  - a running sweep, which visits every sector in order, never satisfies a
    non-monotonic target sequence
  - a target that repeats a sector still matches, which a visited-set
    implementation cannot do
  - arbitrary per-element phases are not a steering angle at all
"""

import math
import sys

sys.path.insert(0, ".")  # importable from project root
from phaser_ctf import CtfMode, fit_ramp, PLACEHOLDER_FLAG

C = 299792458.0
SIGNAL_FREQ = 10.25e9
D = 0.014  # element spacing (m), roughly lambda/2 at X band


def make_ramp(theta_deg, signal_freq=SIGNAL_FREQ, d=D):
    """Reproduce main.js btn-apply-steer, including its rounding to integers."""
    ph_delta = -(360.0 * d * math.sin(math.radians(theta_deg)) * signal_freq / C)
    return [round(k * ph_delta) for k in range(8)]


def convert_phase_to_angle(ph_delta, signal_freq=SIGNAL_FREQ, d=D):
    """Reproduce PhaserHeadless.ConvertPhaseToSteerAngle."""
    value = (C * math.radians(abs(ph_delta))) / (2 * 3.14159 * signal_freq * d)
    theta = math.degrees(math.asin(max(min(1.0, value), -1.0)))
    return theta if ph_delta >= 0 else -theta


def steer(ctf, theta_deg, now):
    ctf.observe(make_ramp(theta_deg), convert_phase_to_angle, now=now)


def hold(ctf, theta_deg, t0, dwell=2.0):
    """Steer somewhere and hold it long enough to be confirmed."""
    steer(ctf, theta_deg, t0)
    ctf.status(now=t0 + dwell + 0.1)
    return t0 + dwell + 0.2


def test_fit_ramp():
    step, ok = fit_ramp([0, -50, -100, -150, -200, -250, -300, -350])
    assert ok, "a clean ramp should fit"
    assert abs(step - (-50)) < 1e-6, step

    # Individually dragged phase sliders are not a ramp and must not be
    # reported as a steering angle.
    _, ok = fit_ramp([0, 90, 15, 200, 5, 170, 60, 300])
    assert not ok, "arbitrary phases should be rejected"
    print("fit_ramp: ok")


def test_sector_is_not_mirrored():
    """The regression test for the sign convention.

    main.js negates the ramp relative to ConvertSteerAngleToPhase. Feed the
    measured step back in without undoing that and every sector mirrors about
    boresight: +30 reads as -30, sector 4 becomes sector 2, and the whole
    thing still looks plausible.
    """
    ctf = CtfMode()
    for theta, expected in ((-60, 1), (-30, 2), (0, 3), (30, 4), (60, 5)):
        steer(ctf, theta, now=0.0)
        got = ctf.status(now=0.0)["data"]["current_sector"]
        assert got == expected, f"steer {theta} deg -> sector {got}, expected {expected}"
    print("sector mapping: ok (not mirrored)")


def test_dwell_required():
    ctf = CtfMode(target=[4], dwell_s=2.0)
    steer(ctf, 30, now=0.0)
    assert not ctf.status(now=1.0)["data"]["matched"], "matched before the dwell elapsed"
    assert ctf.status(now=2.5)["data"]["matched"], "should match once held"
    print("dwell: ok")


def test_sweep_does_not_satisfy_sequence():
    """A sweep walks 1,2,3,4,5 repeatedly. The target is not monotonic, and
    matching on consecutive distinct entries must keep it that way."""
    ctf = CtfMode(target=[3, 1, 4, 1, 2])
    now = 0.0
    for _ in range(4):  # four full sweeps, every sector properly dwelt in
        for theta in (-60, -30, 0, 30, 60):
            now = hold(ctf, theta, now)
    assert not ctf.status(now=now)["data"]["matched"], "a sweep should never match"
    print("sweep immunity: ok")


def test_repeated_sector_sequence():
    """3 1 4 1 2 — sector 1 appears twice, which a visited-set cannot express."""
    ctf = CtfMode(target=[3, 1, 4, 1, 2])
    now = 0.0
    for theta in (0, -60, 30, -60, -30):
        now = hold(ctf, theta, now)

    data = ctf.status(now=now)["data"]
    assert data["matched"], "the exact sequence should match"
    assert data["flag"] == PLACEHOLDER_FLAG, data.get("flag")
    print("repeated-sector sequence: ok")


def test_wrong_turn_costs_progress_but_does_not_lock_out():
    ctf = CtfMode(target=[3, 1, 4, 1, 2])
    now = 0.0
    for theta in (0, -60, 60):          # 3, 1, then a wrong 5
        now = hold(ctf, theta, now)
    assert ctf.status(now=now)["data"]["progress"] == 0, "wrong turn should cost progress"

    for theta in (0, -60, 30, -60, -30):  # start over cleanly
        now = hold(ctf, theta, now)
    assert ctf.status(now=now)["data"]["matched"], "should still be able to finish"
    print("wrong turn: ok")


def test_flag_withheld_in_sim_when_configured():
    ctf = CtfMode(target=[4], flag="flag{real}", allow_sim=False)
    now = hold(ctf, 30, 0.0)

    sim = ctf.status(now=now, sim_mode=True)["data"]
    assert sim["matched"] and "flag" not in sim, "sim must not hand out the flag"
    assert sim["flag_withheld_in_sim"]

    hardware = ctf.status(now=now, sim_mode=False)["data"]
    assert hardware["flag"] == "flag{real}"
    print("sim gating: ok")


def test_between_sectors_is_no_sector():
    ctf = CtfMode()
    steer(ctf, -45, now=0.0)  # halfway between sector 1 and sector 2
    assert ctf.status(now=0.0)["data"]["current_sector"] is None
    print("between sectors: ok")


if __name__ == "__main__":
    test_fit_ramp()
    test_sector_is_not_mirrored()
    test_dwell_required()
    test_sweep_does_not_satisfy_sequence()
    test_repeated_sector_sequence()
    test_wrong_turn_costs_progress_but_does_not_lock_out()
    test_flag_withheld_in_sim_when_configured()
    test_between_sectors_is_no_sector()
    print("\nall ctf-mode tests passed")
