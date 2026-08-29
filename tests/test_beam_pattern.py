"""The beam pattern has to actually form -- and the ADAR has to be told to.

This covers the failure where the FFT showed a healthy HB100 peak but the
beam-pattern plot was a flat line: every steering phase was written to the
ADAR1000's SPI shadow registers and none of them was ever latched into the
beam state, so the array measured the same response at all 162 sweep points.

Nothing caught it because the simulator read the shadow registers directly.
It now models the latch, which is what makes the sweep test below sensitive
to the bug at all -- `test_unlatched_writes_flatten_the_pattern` is the guard
that keeps it that way.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

np = pytest.importorskip("numpy")


def _sim_array():
    import phaser_sim
    return phaser_sim.make_stub_array()


def _headless():
    """A PhaserHeadless wired to the simulator, with no sockets bound.

    Built with __new__ so __init__'s ZMQ binds are skipped; _do_init_hardware
    is the part under test.
    """
    try:
        import adi  # noqa: F401
    except Exception:
        # pyadi-iio binds libiio at import. Sim mode never touches it, so a
        # stub keeps this runnable on a plain dev box.
        import types
        sys.modules.setdefault("adi", types.ModuleType("adi"))
    try:
        import phaser_headless
    except Exception as exc:  # noqa: BLE001 - zmq/msgpack absent
        pytest.skip(f"phaser_headless unavailable: {exc}")

    h = phaser_headless.PhaserHeadless.__new__(phaser_headless.PhaserHeadless)
    h.sim_mode = True
    h.c = 299792458
    h._do_init_hardware()
    return h


def _sweep(h):
    data = h.do_sweep()
    return np.asarray(data["ArrayGain"]), np.asarray(data["ArrayAngle"])


# --- the latch ---------------------------------------------------------

def test_phase_writes_are_latched():
    from ADAR_pyadi_functions import ADAR_set_Phase

    array = _sim_array()
    before = array.latch_count
    ADAR_set_Phase(array, 30.0, 2.8125, [0.0] * 8)

    assert array.latch_count == before + 1, "steering phases were never latched"
    for element in array.elements.values():
        assert element.latched_phase == element.rx_phase


def test_taper_writes_are_latched():
    from ADAR_pyadi_functions import ADAR_set_Taper

    array = _sim_array()
    before = array.latch_count
    ADAR_set_Taper(array, [100, 90, 80, 70, 60, 50, 40, 30])

    assert array.latch_count == before + 1, "taper gains were never latched"
    assert [e.latched_gain for e in array.elements.values()] == [
        100, 90, 80, 70, 60, 50, 40, 30
    ]


def test_a_zeroed_element_is_actually_off():
    from ADAR_pyadi_functions import ADAR_set_Taper

    array = _sim_array()
    ADAR_set_Taper(array, [100, 100, 100, 100, 100, 100, 100, 0])
    assert array.elements[8].rx_attenuator is True
    assert array.elements[8].latched_gain == 0


def test_adar_init_powers_up_the_rx_chain():
    """reset() alone leaves the vector modulator off, and a powered-down
    vector modulator cannot shift phase however many phases we write."""
    from ADAR_pyadi_functions import ADAR_init, ADAR_set_mode

    device = _sim_array().devices["BEAM0"]
    ADAR_init(device)

    assert device.rx_vm_enable is True, "vector modulator off -- no phase shifting"
    assert device.rx_vga_enable is True
    assert device.rx_lna_enable is True
    # Beam state must come from SPI, not from the on-chip RAM sequencer,
    # or the rx_phase writes land somewhere nothing reads.
    assert device.beam_mem_enable is False
    assert device.sequencer_enable is False
    assert device.tr_source == "spi"
    assert device.tr_spi == "rx"

    ADAR_set_mode(device, "rx")
    assert all(c.rx_enable for c in device.channels)


# --- the pattern itself ------------------------------------------------

def test_sim_sweep_produces_a_beam_pattern():
    h = _headless()
    gain, angle = _sweep(h)

    assert len(gain) == len(angle) > 20
    dynamic_range = gain.max() - gain.min()
    assert dynamic_range > 6, (
        f"pattern is flat ({dynamic_range:.2f} dB across the scan) -- "
        "the array is not steering"
    )
    peak_angle = angle[int(np.argmax(gain))]
    assert abs(peak_angle) < 5, (
        f"main lobe at {peak_angle:.1f} deg, but the simulated HB100 is at "
        "boresight"
    )


def test_unlatched_writes_flatten_the_pattern():
    """The guard on the test above.

    If someone makes the simulator read the shadow registers again, the sweep
    test starts passing whether or not the array is latched, and this repo
    loses its only detector for the flat-pattern bug.
    """
    h = _headless()
    h.array.latch_rx_settings = lambda: None  # the pre-fix helpers

    gain, _ = _sweep(h)
    assert gain.max() - gain.min() < 1.0, (
        "simulator no longer models the latch -- the sweep test above is now "
        "blind to an unlatched array"
    )


# --- sweep resolution vs phase quantization ----------------------------

def test_phase_bits_do_not_coarsen_the_sweep():
    """`bits` sets the phase-shifter LSB; `steer_res` sets how finely we scan.

    They were the same attribute, so dropping to a 3-bit phase shifter (a
    45 deg LSB) also dropped the sweep to 45 deg steps -- five points across
    the whole scan, which plots as nothing recognisable.
    """
    h = _headless()
    h.handle_command({"cmd": "set_state", "state": {
        "bits": 3, "steer_res": 1.0, "ignore_res": False,
    }})

    assert h.phase_step == 45.0
    assert h.steer_res == 1.0

    _, angle = _sweep(h)
    assert len(angle) > 100, f"sweep collapsed to {len(angle)} points"
    assert max(abs(np.diff(angle))) <= 1.001


def test_ignore_res_steps_phase_by_one_lsb():
    """Legacy 'ignore steering resolution': walk phase, not angle."""
    h = _headless()
    h.handle_command({"cmd": "set_state", "state": {
        "bits": 7, "ignore_res": True,
    }})

    gain, angle = _sweep(h)
    assert len(angle) > 20
    assert gain.max() - gain.min() > 6
    assert abs(angle[int(np.argmax(gain))]) < 5


# --- calibration actually reaching the hardware ------------------------
#
# phaser_headless loaded phase_cal and channel_cal at init, printed them, and
# then used neither. Legacy applies both -- pcal inside ADAR_set_Phase, ccal
# on the two rx_hardwaregain attributes in SDR_functions -- and phaser_service
# applies both at the caller. phaser_headless was the outlier on each.

def test_phase_calibration_reaches_the_array():
    h = _headless()
    h.phase_cal = [0.0, 12.0, -7.5, 30.0, 3.25, -18.0, 44.0, -1.5]
    h.phaseList = [0.0] * 8

    from ADAR_pyadi_functions import ADAR_set_Phase
    ADAR_set_Phase(h.array, 0.0, h.phase_step, h._apply_phase_cal(h.phaseList))

    got = [h.array.elements[i + 1].latched_phase for i in range(8)]
    assert got == pytest.approx([c % 360 for c in h.phase_cal]), (
        "phase calibration never reached the elements"
    )


def _pattern_quality(gain, angle):
    """Peak level, where it points, and how far the sidelobes sit below it.

    Sidelobe level is the metric that matters for phase calibration. Element
    phase errors barely move the peak -- they fill in the nulls and lift the
    sidelobes, which is what turns a beam pattern into a shapeless blob.
    """
    peak = gain.max()
    return peak, angle[int(np.argmax(gain))], peak - gain[np.abs(angle) > 20].max()


def test_phase_calibration_recovers_a_smeared_pattern():
    """The whole point of pcal: eight elements that add coherently.

    The simulated array is given per-element phase errors; loading the
    matching calibration has to restore the pattern. Both runs see the same
    seeded noise sequence, so the comparison is exact.
    """
    errors = [0.0, 37.0, -22.0, 15.0, 48.0, -31.0, 9.0, 25.0]

    uncal = _headless()
    uncal.array.element_phase_error = list(errors)
    uncal.phase_cal = [0.0] * 8
    peak_u, angle_u, sll_u = _pattern_quality(*_sweep(uncal))

    cal = _headless()
    cal.array.element_phase_error = list(errors)
    cal.phase_cal = list(errors)
    peak_c, angle_c, sll_c = _pattern_quality(*_sweep(cal))

    assert sll_u < 8, (
        f"uncalibrated sidelobes are already {sll_u:.1f} dB down -- the "
        "simulated array is too well matched for this test to mean anything"
    )
    assert sll_c > 10, (
        f"sidelobes only {sll_c:.1f} dB down with calibration loaded; "
        "the pattern is still smeared"
    )
    assert sll_c > sll_u + 3
    assert peak_c > peak_u, "calibration did not recover array gain"
    assert abs(angle_c) < 1.5


def test_channel_calibration_reaches_the_rx_gains():
    h = _headless()
    assert (
        h.sdr.rx_hardwaregain_chan1 - h.sdr.rx_hardwaregain_chan0
        == pytest.approx(h.channel_cal[1] - h.channel_cal[0])
    )

    h.channel_cal = [-2.0, 3.0]
    h.set_rx_gain(20)
    assert h.sdr.rx_hardwaregain_chan0 == 18
    assert h.sdr.rx_hardwaregain_chan1 == 23


def test_taper_reaches_full_scale():
    """UI taper is 0-100; the rx_gain register is 0-127."""
    h = _headless()
    h.gain_cal = [1.0] * 8
    assert h._apply_gain_cal([100] * 8) == [127] * 8
    assert h._apply_gain_cal([0] * 8) == [0] * 8


def test_coarse_phase_bits_do_not_quantize_the_calibration():
    """`bits` coarsens the steering ramp, not the per-element correction."""
    from ADAR_pyadi_functions import ADAR_set_Phase

    array = _sim_array()
    offsets = [1.4, -2.6, 3.1, 0.0, -4.7, 5.2, 0.9, -1.1]
    ADAR_set_Phase(array, 0.0, 45.0, offsets)  # 3-bit phase shifter

    got = [array.elements[i + 1].latched_phase for i in range(8)]
    assert got == pytest.approx([o % 360 for o in offsets]), (
        "a coarse bits setting rounded the calibration away"
    )


# --- the monopulse phase branch cut ------------------------------------

def test_beam_phase_stays_within_one_turn():
    """beam_phase must be an angle, not a difference of two angles.

    do_sweep() used to compute `np.angle(sum) - np.angle(delta)`, which spans
    (-2pi, 2pi). The quantity is only meaningful mod 2pi, and the very next
    line takes sign() of it to build the monopulse error curve -- so the same
    physical angle reported as +pi/2 or -3pi/2 flipped the curve's sign.

    Roughly a quarter of sweep points exceeded pi under the old expression, so
    this assertion is a hard guard rather than a statistical one.
    """
    h = _headless()
    phase = np.asarray(h.do_sweep()["PhaseDiff"])

    over = np.abs(phase) > np.pi + 1e-9
    assert not over.any(), (
        f"{over.sum()} of {over.size} sweep points reported |beam_phase| > pi. "
        "beam_phase must be angle(sum * conj(delta)), not "
        "angle(sum) - angle(delta)."
    )


def test_beam_phase_is_insensitive_to_the_branch_cut():
    """The adversarial case, stated directly.

    A sum sample sitting on the negative real axis is what argmax over a
    flat-envelope CW tone lands on a quarter of the time. Nudging its
    imaginary part by 1e-12 moves np.angle by a full 2pi, which used to invert
    the monopulse error. The product form cannot see that nudge.
    """
    delta = np.exp(1j * np.pi / 2)

    phases = [
        np.angle(complex(-1.0, im) * np.conj(delta)) for im in (+1e-12, -1e-12)
    ]
    assert phases[0] == pytest.approx(phases[1], abs=1e-9), (
        "beam_phase moved when the sum sample crossed the negative real axis"
    )
    assert np.sign(phases[0]) == np.sign(phases[1]), "monopulse error sign flipped"

    # And the old expression really did break here -- if this ever stops being
    # true the test above has lost its teeth.
    legacy = [np.angle(complex(-1.0, im)) - np.angle(delta) for im in (+1e-12, -1e-12)]
    assert np.sign(legacy[0]) != np.sign(legacy[1]), (
        "the legacy expression no longer demonstrates the bug; "
        "this test is no longer guarding anything"
    )


def test_beam_phase_does_not_depend_on_which_sample_the_peak_lands_on():
    """sum and delta share the carrier, so it cancels in the product.

    This is the property that makes the value well defined at all: max_index is
    an argmax over a constant-envelope tone, so which sample wins is decided by
    noise. Whatever it picks, beam_phase must come out the same.

    Run without noise, and at a steering ramp where NEITHER beam is nulled. Two
    ramps are degenerate and would prove nothing: 0 degrees nulls delta (the
    sub-arrays are identical), and 45 degrees nulls sum (a 45-degree per-element
    ramp makes sub-array 1 the exact negative of sub-array 0). At a null the
    product is zero and its angle is rounding noise either way.

    20 degrees is chosen because both beams are healthy there AND the legacy
    expression visibly breaks, so one configuration demonstrates both halves.
    """
    import phaser_sim

    # _headless() stubs `adi`, which SDR_functions imports at module level.
    h = _headless()
    from ADAR_pyadi_functions import ADAR_set_Phase
    from SDR_functions import SDR_getData

    original = phaser_sim.SimSDR.NOISE_SIGMA
    phaser_sim.SimSDR.NOISE_SIGMA = 0.0
    try:
        ADAR_set_Phase(h.array, 20.0, 2.8125, [0.0] * 8)
        data = SDR_getData(h.sdr)
        chan1, chan2 = np.asarray(data[0]), np.asarray(data[1])
    finally:
        phaser_sim.SimSDR.NOISE_SIGMA = original

    sum_chan, delta_chan = chan1 + chan2, chan1 - chan2
    assert np.abs(sum_chan).mean() > 100, "sum beam is nulled; pick another ramp"
    assert np.abs(delta_chan).mean() > 100, "delta beam is nulled; pick another ramp"

    phases = np.angle(sum_chan * np.conj(delta_chan))
    spread = np.ptp(phases)
    assert spread < 1e-6, (
        f"beam_phase varied by {spread:.3e} rad across samples; it must not "
        "depend on which sample argmax happens to pick"
    )

    # The legacy expression is not sample-independent: it jumps by 2*pi
    # wherever the two angles straddle the branch cut.
    legacy = np.angle(sum_chan) - np.angle(delta_chan)
    assert np.mean(np.abs(legacy) > np.pi) > 0.1, (
        "the legacy expression no longer shows the branch-cut jumps; "
        "this test is no longer guarding anything"
    )
