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
