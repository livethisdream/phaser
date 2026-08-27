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
