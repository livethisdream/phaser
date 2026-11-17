"""Monopulse tracking (ADI Lab 8) closes the loop on the error beam.

Tracking used to be an ordinary beam sweep whose Tracking tab plotted each
sweep's sum-beam peak: nothing read the delta beam. ADI's legacy track() held
the beam, read sum and delta there, and stepped the steering phase one LSB
toward the side the monopulse error pointed. These tests hold do_sweep() to
that loop in the simulator, with noise off so the walk is deterministic.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

np = pytest.importorskip("numpy")


@pytest.fixture
def h(monkeypatch):
    import types
    sys.modules.setdefault("adi", types.ModuleType("adi"))
    try:
        import phaser_headless
        import phaser_sim
    except Exception as exc:  # noqa: BLE001 - zmq/msgpack absent
        pytest.skip(f"phaser_headless unavailable: {exc}")
    monkeypatch.setattr(phaser_sim.SimSDR, "NOISE_SIGMA", 0.0)
    inst = phaser_headless.PhaserHeadless.__new__(phaser_headless.PhaserHeadless)
    inst.sim_mode = True
    inst.c = 299792458
    inst._do_init_hardware()
    return inst


def _set(h, **state):
    h.handle_command({"cmd": "set_state", "data": {"state": state}})


def _track(h, start, target, updates):
    _set(h, mode="Tracking", sim_target_angle_deg=start)
    acquire = h.do_sweep()
    _set(h, sim_target_angle_deg=target)
    return acquire, [h.do_sweep() for _ in range(updates)]


def test_error_is_signed_by_side_and_linear_near_the_beam(h):
    """|delta|/|sum|, positive for a source on the +theta side (TRACK_DIRECTION)."""
    _set(h, mode="Static Phase", hold_angle=0.0)
    err = {}
    for src in (-5.0, -3.0, -1.0, 1.0, 3.0, 5.0):
        _set(h, sim_target_angle_deg=src)
        err[src] = h.do_sweep()["ErrorFunc"][0]
    assert all(err[s] > 0 for s in (1.0, 3.0, 5.0))
    assert all(err[s] < 0 for s in (-1.0, -3.0, -5.0))
    assert err[3.0] == pytest.approx(-err[-3.0], abs=1e-9)
    # Near-linear: three times the offset, about three times the error.
    assert 2.5 < err[3.0] / err[1.0] < 3.5


def test_error_does_not_move_with_signal_level(h):
    """The old dB-ratio error changed with the level; a linear ratio cannot.

    Turning every element down together drops both beams by the same dB.
    """
    _set(h, mode="Static Phase", hold_angle=0.0, sim_target_angle_deg=4.0)
    a = h.do_sweep()
    _set(h, gainList=[40] * 8)
    b = h.do_sweep()
    assert a["ArrayGain"][0] - b["ArrayGain"][0] > 5
    assert a["ErrorFunc"][0] == pytest.approx(b["ErrorFunc"][0], abs=1e-3)


def test_first_update_acquires_with_a_sweep(h):
    acquire, held = _track(h, -18.0, -18.0, 1)
    assert len(acquire["ArrayGain"]) > 20
    assert acquire["track_error"] is None
    assert abs(acquire["track_angle_deg"] - (-18.0)) < 1.0
    assert len(held[0]["ArrayGain"]) == 1
    assert held[0]["track_error"] is not None


@pytest.mark.parametrize("start,target", [(5.0, 15.0), (-15.0, -25.0)])
def test_converges_on_a_moved_source_and_holds(h, start, target):
    _, held = _track(h, start, target, 40)
    angles = [r["track_angle_deg"] for r in held]
    lock = next(i for i, a in enumerate(angles) if abs(a - target) <= 1.0)
    # Ten degrees at one LSB (about 0.9 deg) an update.
    assert lock <= 15, f"took {lock} updates to reach {target}"
    # Then it dithers one LSB either side of the source and stays there.
    assert max(abs(a - target) for a in angles[lock:]) < 1.5


def test_follows_a_moving_source(h):
    _set(h, mode="Tracking", sim_target_angle_deg=0.0)
    h.do_sweep()
    lag = []
    for i in range(120):
        src = min(30.0, 0.25 * i)
        _set(h, sim_target_angle_deg=src)
        lag.append(h.do_sweep()["track_angle_deg"] - src)
    assert max(abs(x) for x in lag) < 2.0


def test_leaving_the_mode_drops_the_lock(h):
    _track(h, 10.0, 10.0, 3)
    _set(h, mode="Beam Sweep")
    out = h.do_sweep()
    assert out["track_angle_deg"] is None and h.track_phase is None
    _set(h, mode="Tracking")
    assert len(h.do_sweep()["ArrayGain"]) > 20
