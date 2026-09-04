"""Synthetic tests for phaser_ctf.CtfMode — no hardware, no backend needed.

Builds phase ramps exactly the way `frontend/src/main.js` builds them, feeds
them through the same conversion the backend uses, and checks the sector
state machine behaves under the cases that are easy to get wrong:

  - a positive steer reads as the positive-side sector, not its mirror
  - a sector only counts once it has been held for the dwell
  - a running sweep, which visits every sector in order, never satisfies a
    non-monotonic target sequence
  - a target that repeats a sector still matches, which a visited-set
    implementation cannot do
  - arbitrary per-element phases are not a steering angle at all

The commanded tests below construct their CtfMode with source="commanded"
explicitly. The shipped default is "tracked" -- the con challenge is a player
walking an HB100 in front of the array -- so a bare CtfMode() ignores
`observe()` entirely, and these tests would silently pass by never scoring
anything.
"""

import math
import sys
from pathlib import Path

# Also runnable directly (python tests/test_phaser_ctf.py); pytest gets the
# root from pythonpath = ["."] in pyproject.toml.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from phaser_ctf import (
    CtfMode, fit_ramp, peak_angle_centroid, PLACEHOLDER_FLAG,
    DEFAULT_SECTOR_CENTRES_DEG, DEFAULT_TOLERANCE_DEG,
)

# One real sweep excerpt off the array (HB100 lit, Rx_gain 10, 10.42 GHz):
# 25 grid points either side of the mainlobe peak, verbatim. The point of
# keeping real numbers rather than a clean synthetic lobe is that the top of
# this one is CLIPPED and ragged -- gains hover around -0.3 dB across four
# degrees with a single 0.69 dB sample sticking up -- which is exactly the
# shape that makes an argmax useless.
REAL_LOBE_ANGLES = [
    -12.98, -12.04, -11.10, -10.17, -9.23, -8.30, -7.38, -6.45, -5.53,
    -4.60, -3.68, -2.76, -1.84, -0.92, 0.00, 0.92, 1.84, 2.76, 3.68,
    4.60, 5.53, 6.45, 7.38, 8.30, 9.23,
]
REAL_LOBE_GAINS = [
    -10.36, -8.30, -6.68, -5.55, -4.23, -3.32, -2.85, -1.42, -1.39,
    -0.52, -0.30, -0.43, 0.69, -0.27, -0.11, -0.46, -0.36, -0.61, -0.82,
    -1.74, -2.04, -2.64, -3.13, -4.30, -5.22,
]

C = 299792458.0
SIGNAL_FREQ = 10.25e9
D = 0.014  # element spacing (m), roughly lambda/2 at X band


def armed(**kwargs):
    """A CtfMode in the state the panel's Start button leaves it: cleared and ARMED.

    CtfMode is deliberately unarmed on construction, so a bare CtfMode() scores
    nothing no matter how long the beam sits somewhere. Every test that expects
    progress has to start the session first, exactly as a player does.
    """
    kwargs.setdefault("source", "commanded")
    ctf = CtfMode(**kwargs)
    ctf.reset()
    return ctf


def tracked(**kwargs):
    """A CtfMode scoring the measured peak angle, cleared and ARMED."""
    kwargs.setdefault("source", "tracked")
    ctf = CtfMode(**kwargs)
    ctf.reset()
    return ctf


def see(ctf, angle_deg, sweeps=1, signal_db=0.0):
    """Deliver `sweeps` identical sweep observations of a source at an angle.

    signal_db defaults to 0 (full scale), which is roughly where the HB100
    actually sits at the table.
    """
    for _ in range(sweeps):
        ctf.observe_tracked(angle_deg, signal_db)


def angle_of(sector):
    """Centre angle of a 1-based sector, read from the module under test.

    Tests say "sector 4", not "+20 degrees". Geometry has already moved once
    (from +/-60 centres at +/-12 tolerance to +/-40 at +/-5, to keep every
    window inside the +/-45 where the beam still shows a clear peak), and
    hardcoded angles would have silently retargeted a different sector rather
    than failing.
    """
    return DEFAULT_SECTOR_CENTRES_DEG[sector - 1]


def between_sectors(low, high):
    """An angle in the dead band between two adjacent sectors."""
    return (DEFAULT_SECTOR_CENTRES_DEG[low - 1] + DEFAULT_SECTOR_CENTRES_DEG[high - 1]) / 2


def dead_band_beside(sector):
    """A genuine no-sector angle next to `sector`.

    between_sectors() only lands in a dead band for ADJACENT sectors. The
    midpoint of 1 and 3 is -20, which is the dead CENTRE of sector 2 -- using
    it as a "crossing" silently scores sector 2 and corrupts the trail.
    """
    n = len(DEFAULT_SECTOR_CENTRES_DEG)
    other = sector + 1 if sector < n else sector - 1
    return between_sectors(min(sector, other), max(sector, other))


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
boresight: a positive steer reads negative, sector 4 becomes sector 2,
    and the whole thing still looks plausible.
    """
    ctf = armed()
    for expected in range(1, len(DEFAULT_SECTOR_CENTRES_DEG) + 1):
        steer(ctf, angle_of(expected), now=0.0)
        got = ctf.status(now=0.0)["data"]["current_sector"]
        assert got == expected, f"steer {theta} deg -> sector {got}, expected {expected}"
    print("sector mapping: ok (not mirrored)")


def test_dwell_required():
    ctf = armed(target=[4], dwell_s=2.0)
    steer(ctf, angle_of(4), now=0.0)
    assert not ctf.status(now=1.0)["data"]["matched"], "matched before the dwell elapsed"
    assert ctf.status(now=2.5)["data"]["matched"], "should match once held"
    print("dwell: ok")


def test_sweep_does_not_satisfy_sequence():
    """A sweep walks 1,2,3,4,5 repeatedly. The target is not monotonic, and
    matching on consecutive distinct entries must keep it that way."""
    ctf = armed(target=[3, 1, 4, 1, 2])
    now = 0.0
    for _ in range(4):  # four full sweeps, every sector properly dwelt in
        for theta in DEFAULT_SECTOR_CENTRES_DEG:
            now = hold(ctf, theta, now)
    assert not ctf.status(now=now)["data"]["matched"], "a sweep should never match"
    print("sweep immunity: ok")


def test_repeated_sector_sequence():
    """3 1 4 1 2 — sector 1 appears twice, which a visited-set cannot express."""
    ctf = armed(target=[3, 1, 4, 1, 2])
    now = 0.0
    for sector in (3, 1, 4, 1, 2):
        now = hold(ctf, angle_of(sector), now)

    data = ctf.status(now=now)["data"]
    assert data["matched"], "the exact sequence should match"
    assert data["flag"] == PLACEHOLDER_FLAG, data.get("flag")
    print("repeated-sector sequence: ok")


def test_wrong_turn_costs_progress_but_does_not_lock_out():
    ctf = armed(target=[3, 1, 4, 1, 2])
    now = 0.0
    for sector in (3, 1, 5):            # 3, 1, then a wrong 5
        now = hold(ctf, angle_of(sector), now)
    assert ctf.status(now=now)["data"]["progress"] == 0, "wrong turn should cost progress"

    for sector in (3, 1, 4, 1, 2):      # start over cleanly
        now = hold(ctf, angle_of(sector), now)
    assert ctf.status(now=now)["data"]["matched"], "should still be able to finish"
    print("wrong turn: ok")


def test_flag_withheld_in_sim_when_configured():
    ctf = armed(target=[4], flag="flag{real}", allow_sim=False)
    now = hold(ctf, angle_of(4), 0.0)

    sim = ctf.status(now=now, sim_mode=True)["data"]
    assert sim["matched"] and "flag" not in sim, "sim must not hand out the flag"
    assert sim["flag_withheld_in_sim"]

    hardware = ctf.status(now=now, sim_mode=False)["data"]
    assert hardware["flag"] == "flag{real}"
    print("sim gating: ok")


def test_between_sectors_is_no_sector():
    ctf = armed()
    probe = between_sectors(1, 2)
    steer(ctf, probe, now=0.0)
    assert ctf.status(now=0.0)["data"]["current_sector"] is None
    # The dead band has to be real, not a rounding artefact: the probe must sit
    # outside both neighbouring windows by a clear margin.
    gap = abs(probe - angle_of(1)) - DEFAULT_TOLERANCE_DEG
    assert gap > 1.0, f"only {gap:.1f} deg outside sector 1 -- widen the dead band"
    print("between sectors: ok")



def test_sector_geometry_stays_inside_the_usable_steer_range():
    """The +/-45 limit is physical, and the sector count is contractual.

    Past ~45 deg an 8-element array's beamwidth broadens as 1/cos(theta): the
    mainlobe stops presenting a clear peak, so a player cannot see which sector
    they are in. Every window edge must therefore stay inside 45.

    The count cannot be traded away to buy room. "3 1 4 1 2" is plaintext in
    another challenge's payload, so five sectors is fixed by prior commitment --
    a future squeeze has to come out of spacing, never out of a sector.
    """
    centres = DEFAULT_SECTOR_CENTRES_DEG
    tol = DEFAULT_TOLERANCE_DEG

    assert len(centres) == 5, "the shipped sequence needs exactly five sectors"

    for i, centre in enumerate(centres):
        assert abs(centre) + tol <= 45.0, \
            f"sector {i + 1} reaches {abs(centre) + tol:.1f} deg, past the usable range"

    # Adjacent windows must not touch, or sector_for_angle -- which returns the
    # FIRST match -- would silently resolve the overlap in favour of the lower
    # sector instead of reporting "between sectors".
    for i in range(len(centres) - 1):
        gap = (centres[i + 1] - tol) - (centres[i] + tol)
        assert gap > 0, f"sectors {i + 1} and {i + 2} overlap by {-gap:.1f} deg"

    print("geometry: ok (5 sectors, all windows inside +/-45, no overlap)")


def test_resting_position_does_not_score_before_start():
    """The bug this gate exists for.

    The frontend pushes a phaseList when it connects. Before arming, that must
    score nothing -- otherwise whatever sector the beam is parked in is a free
    first element. Boresight is the usual culprit (it is sector 3, and the real
    sequence opens with 3), but at a busy table the parked sector is really
    just wherever the last player left the array.
    """
    ctf = CtfMode(target=[3, 1, 4, 1, 2], source="commanded")   # NOT armed: no Start pressed
    steer(ctf, angle_of(3), now=0.0)                # boresight == sector 3
    data = ctf.status(now=60.0)["data"]
    assert data["armed"] is False
    assert data["progress"] == 0, "an unstarted session scored the resting position"
    assert data["current_sector"] == 3, "should still REPORT where the beam is"
    print("unarmed session scores nothing: ok")


def test_start_while_already_in_the_first_sector_counts():
    """Arming must not demand a transition.

    If it did, a sequence beginning with the sector the beam already occupies
    could only be started by steering away and coming back. Pressing Start
    restarts the dwell clock, so holding still is a legitimate way to enter the
    first sector.
    """
    ctf = CtfMode(target=[3, 1], source="commanded")
    steer(ctf, angle_of(3), now=0.0)                # sitting in sector 3 already
    ctf.reset()                                     # Start, without moving
    steer(ctf, angle_of(3), now=0.1)                # same position, still held
    assert ctf.status(now=3.0)["data"]["progress"] == 1, \
        "holding the first sector through the dwell after Start should count it"
    steer(ctf, angle_of(1), now=3.0)
    assert ctf.status(now=6.0)["data"]["matched"], "sequence should complete"
    print("Start while already in the first sector: ok")


def test_reset_clears_progress_and_stays_armed():
    """Start is also Restart: it clears the trail but leaves the session live."""
    ctf = armed(target=[3, 1, 4, 1, 2])
    steer(ctf, angle_of(3), now=0.0)
    assert ctf.status(now=3.0)["data"]["progress"] == 1
    ctf.reset()
    data = ctf.status(now=3.1)["data"]
    assert data["armed"] is True
    assert data["progress"] == 0
    print("restart clears progress, stays armed: ok")

def test_centroid_beats_argmax_on_a_real_clipped_lobe():
    """The measurement the whole tracked challenge is scored on.

    On this real lobe the argmax lands on -1.84 deg purely because one sample
    spiked to 0.69 dB while its neighbours sat near -0.3. The centroid weighs
    the whole mainlobe instead and lands near -0.6.
    """
    imax = max(range(len(REAL_LOBE_GAINS)), key=lambda k: REAL_LOBE_GAINS[k])
    argmax_angle = REAL_LOBE_ANGLES[imax]
    centroid = peak_angle_centroid(REAL_LOBE_ANGLES, REAL_LOBE_GAINS)

    assert argmax_angle == -1.84, "fixture changed; the argmax case is the point"
    assert abs(centroid - argmax_angle) > 1.0, \
        "centroid tracked the argmax spike instead of the lobe"
    assert -1.6 < centroid < 0.4, f"centroid landed at {centroid}, off the lobe"
    print("centroid vs argmax on real data: ok (%.2f vs %.2f)"
          % (centroid, argmax_angle))


def test_centroid_ignores_a_sidelobe():
    """A strong sidelobe outside the -3 dB mainlobe must not drag the answer.

    The walk stops at the first sample past the drop, so a second peak of
    comparable height 30 deg away is never entered.
    """
    clean = peak_angle_centroid(REAL_LOBE_ANGLES, REAL_LOBE_GAINS)
    with_lobe = peak_angle_centroid(
        REAL_LOBE_ANGLES + [30.0, 31.0, 32.0],
        REAL_LOBE_GAINS + [0.5, 0.6, 0.5],
    )
    assert abs(with_lobe - clean) < 1e-9, \
        f"a sidelobe moved the centroid from {clean} to {with_lobe}"
    print("centroid ignores a sidelobe: ok")


def test_centroid_rejects_degenerate_input():
    """No sweep, no angle. None is the value the tracker treats as 'no source'."""
    assert peak_angle_centroid([], []) is None
    assert peak_angle_centroid([1.0, 2.0], [1.0]) is None
    assert peak_angle_centroid(None, None) is None
    print("centroid degenerate input: ok")


def test_tracked_confirms_on_consecutive_sweeps():
    """The tracked dwell is counted in sweeps, not seconds."""
    ctf = tracked(target=[4])
    see(ctf, angle_of(4), sweeps=ctf.track_sweeps - 1)
    assert not ctf.status()["data"]["matched"], "confirmed early"
    see(ctf, angle_of(4))
    assert ctf.status()["data"]["matched"], "should confirm on the Nth sweep"
    print("tracked dwell counts sweeps: ok")


def test_polling_cannot_confirm_a_tracked_sector():
    """status() must not advance the tracked machine.

    The commanded source is clock-driven and confirmed by the panel's poll.
    If that path stayed wired up for tracking, a 700 ms poll would confirm a
    sector the array had only seen once, and the whole sweep-count rule would
    be decorative.
    """
    ctf = tracked(target=[4])
    see(ctf, angle_of(4))                      # one sweep only
    for _ in range(50):
        ctf.status()
    assert not ctf.status()["data"]["matched"], "polling confirmed a sector"
    print("polling does not confirm tracked sectors: ok")


def test_tracked_ignores_a_peak_below_the_signal_floor():
    """A noise-floor peak is not a source and must not read as a sector."""
    ctf = tracked(target=[3])
    see(ctf, angle_of(3), sweeps=10, signal_db=ctf.signal_floor_db - 1.0)
    data = ctf.status()["data"]
    assert data["current_sector"] is None, "scored a sector off the noise floor"
    assert not data["matched"]
    print("signal floor rejects a weak peak: ok")


def test_dead_band_crossing_resets_the_tracked_count():
    """Carrying the source between sectors must not accumulate progress.

    Measured on the array, a hand-carried crossing of a 10 deg dead band took
    11.5 s (11 sweeps) and reported no sector throughout.
    """
    ctf = tracked(target=[2, 3])
    see(ctf, angle_of(2), sweeps=ctf.track_sweeps)      # sector 2 confirmed
    see(ctf, between_sectors(2, 3), sweeps=11)          # the crossing
    assert ctf.status()["data"]["current_sector"] is None
    see(ctf, angle_of(3), sweeps=ctf.track_sweeps - 1)
    assert not ctf.status()["data"]["matched"], "dead band leaked into the count"
    see(ctf, angle_of(3))
    assert ctf.status()["data"]["matched"]
    print("dead band resets the sweep count: ok")


def test_sources_do_not_cross_feed():
    """Exactly one source scores. Both directions."""
    t = tracked(target=[4])
    steer(t, angle_of(4), now=0.0)                      # commanded input
    t.status()
    assert t.status()["data"]["current_sector"] is None, \
        "a tracked run scored a commanded steer"

    c = armed(target=[4])
    see(c, angle_of(4), sweeps=10)                      # tracked input
    assert not c.status(now=99.0)["data"]["matched"], \
        "a commanded run scored a tracked observation"
    print("sources do not cross-feed: ok")


def test_stopped_sweep_reports_nothing_live():
    """A stale reading must not be served as a live one.

    The tracked source is fed by the sweep loop. With the sweep stopped the
    last angle is where the source WAS, and the plot cannot say otherwise --
    a static chart with the bands still drawn looks like a live plot of a
    stationary source.
    """
    ctf = tracked(target=[3])
    see(ctf, angle_of(3))
    live = ctf.status()["data"]
    assert live["measuring"] is True
    assert live["current_sector"] == 3

    stopped = ctf.status(sweeping=False)["data"]
    assert stopped["measuring"] is False
    assert stopped["current_angle_deg"] is None, "served a stale angle as live"
    assert stopped["current_sector"] is None
    assert stopped["holding"] is None
    print("stopped sweep reports nothing live: ok")


def test_confirmation_does_not_span_a_stopped_sweep():
    """A sweep-count chain cannot bridge an interval nothing observed."""
    ctf = tracked(target=[3])
    see(ctf, angle_of(3), sweeps=ctf.track_sweeps - 1)   # one short
    ctf.status(sweeping=False)                            # sweep stops
    see(ctf, angle_of(3))                                 # resumes, same sector
    assert not ctf.status()["data"]["matched"], \
        "the count bridged an interval where nothing was watching"
    see(ctf, angle_of(3), sweeps=ctf.track_sweeps - 1)
    assert ctf.status()["data"]["matched"], "should confirm once seen again"
    print("confirmation does not span a stopped sweep: ok")


def test_stopped_sweep_keeps_earned_progress():
    """Only the in-flight confirmation is dropped; the trail is untouched."""
    ctf = tracked(target=[3, 1])
    see(ctf, angle_of(3), sweeps=ctf.track_sweeps)        # sector 3 confirmed
    assert ctf.status()["data"]["progress"] == 1
    assert ctf.status(sweeping=False)["data"]["progress"] == 1, \
        "a stopped sweep erased progress the player had already earned"
    print("stopped sweep keeps earned progress: ok")


def test_commanded_source_ignores_the_sweep_flag():
    """The commanded source is fed by set_state, not the sweep loop.

    Guards the fallback path: it must keep scoring with the sweep stopped.
    """
    ctf = armed(target=[4], dwell_s=2.0)
    steer(ctf, angle_of(4), now=0.0)
    data = ctf.status(now=2.5, sweeping=False)["data"]
    assert data["measuring"] is True, "commanded source should not care"
    assert data["matched"], "a stopped sweep must not block the commanded path"
    print("commanded source ignores the sweep flag: ok")


def test_tracked_full_sequence_with_dead_bands():
    """The whole challenge, walked the way a player actually walks it."""
    ctf = tracked(target=[3, 1, 4, 1, 2], flag="flag{tracked}")
    walk = [3, 1, 4, 1, 2]
    for i, sector in enumerate(walk):
        if i:
            # Leave the previous sector through a real dead band before
            # arriving at the next one, the way a carried horn does.
            see(ctf, dead_band_beside(walk[i - 1]), sweeps=4)
        see(ctf, angle_of(sector), sweeps=ctf.track_sweeps)
    data = ctf.status()["data"]
    assert data["matched"], "the walked sequence should complete"
    assert data["flag"] == "flag{tracked}"
    print("tracked full sequence: ok")


if __name__ == "__main__":
    test_fit_ramp()
    test_sector_is_not_mirrored()
    test_dwell_required()
    test_sweep_does_not_satisfy_sequence()
    test_repeated_sector_sequence()
    test_wrong_turn_costs_progress_but_does_not_lock_out()
    test_flag_withheld_in_sim_when_configured()
    test_between_sectors_is_no_sector()
    test_sector_geometry_stays_inside_the_usable_steer_range()
    test_resting_position_does_not_score_before_start()
    test_start_while_already_in_the_first_sector_counts()
    test_reset_clears_progress_and_stays_armed()
    test_centroid_beats_argmax_on_a_real_clipped_lobe()
    test_centroid_ignores_a_sidelobe()
    test_centroid_rejects_degenerate_input()
    test_tracked_confirms_on_consecutive_sweeps()
    test_polling_cannot_confirm_a_tracked_sector()
    test_tracked_ignores_a_peak_below_the_signal_floor()
    test_dead_band_crossing_resets_the_tracked_count()
    test_sources_do_not_cross_feed()
    test_stopped_sweep_reports_nothing_live()
    test_confirmation_does_not_span_a_stopped_sweep()
    test_stopped_sweep_keeps_earned_progress()
    test_commanded_source_ignores_the_sweep_flag()
    test_tracked_full_sequence_with_dead_bands()
    print("\nall ctf-mode tests passed")
