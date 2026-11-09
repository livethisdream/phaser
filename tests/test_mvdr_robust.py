"""MVDR keeps its null on the interferer when the kit's channels mismatch.

Neil's kit, 2026-11-09: HB100 at boresight, beam held at 0 deg, the synthetic
interferer at +20 deg and -10 dBFS, MVDR at K=128 and load 1e-3. MVDR put its
one digital null near +10 deg, slid the beam to -3 deg and raised the response
at +20 deg, so Interferer vs Target got worse and MVDR Suppression read
negative. At 0 dBFS it nulled +20 deg as it should.

The cause is the look vector. MVDR assumed the ideal [1, 1], but the kit's two
Rx paths are not in phase, so the real target arrives as [1, e^(j*eps)] while
the synthetic interferer, added after the read, is ideal. With the target the
stronger signal, MVDR spent the null on the target's off-look component.
SimSDR.channel_phase_error_deg reproduces the mismatch; _mvdr_look_vector
measures [1, r] on the target and fixes it.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

np = pytest.importorskip("numpy")

CHANNEL_ERROR_DEG = 30.0


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


def _tone_db(d, hz):
    xf = np.asarray(d["xf"])
    g = np.asarray(d["max_gain"])
    i = int(np.argmin(np.abs(xf - hz)))
    return float(g[max(0, i - 3):i + 4].max())


def _ivt(h, bf_mode):
    """Interferer vs Target as the GUI's tile reads it, and the last result."""
    _set(h, bfMode=bf_mode)
    h._last_fft_t = None  # held modes throttle the spectrum; force it
    d = h.do_sweep()
    return _tone_db(d, d["interferer_freq_hz"]) - _tone_db(d, d["target_tone_hz"]), d


def _scene(h, power_dbfs, channel_error_deg=CHANNEL_ERROR_DEG):
    h.sdr.channel_phase_error_deg = channel_error_deg
    _set(h, mode="Signal vs Time", hold_angle=0.0,
         interferer_enable=True, interferer_angle_deg=20.0,
         interferer_power_dbfs=power_dbfs, interferer_offset_hz=100e3,
         mvdrK=128, mvdrDiagLoad=1e-3)


def _suppression(h):
    manual, _ = _ivt(h, "manual")
    mvdr, d = _ivt(h, "mvdr")
    pat = d["adaptive_pattern"]
    at20 = pat["adaptive_db"][int(np.argmin(np.abs(np.asarray(pat["angles"]) - 20.0)))]
    return manual - mvdr, at20


def test_ideal_look_vector_self_nulls_like_the_kit(h, monkeypatch):
    """The old behavior, reproduced: a weak interferer is not nulled."""
    monkeypatch.setattr(type(h), "_mvdr_look_vector",
                        lambda self, a, b: np.ones(2, dtype=np.complex128))
    _scene(h, -10.0)
    supp, at20 = _suppression(h)
    assert supp < 0, supp            # Neil read -3 to -10 dB
    assert at20 > -20, at20          # +20 deg is not in a null
    _scene(h, 0.0)
    supp, _ = _suppression(h)
    assert supp > 10, supp           # a dominant interferer still gets the null


@pytest.mark.parametrize("power_dbfs", [-10.0, 0.0])
def test_measured_look_vector_nulls_the_interferer(h, power_dbfs):
    _scene(h, power_dbfs)
    supp, at20 = _suppression(h)
    assert supp > 10, supp
    assert at20 < -25, at20


def test_look_vector_measures_the_channel_error(h):
    _scene(h, -10.0)
    data = h.sdr.rx()
    look = h._mvdr_look_vector(data[0], data[1])
    assert np.degrees(np.angle(look[1])) == pytest.approx(CHANNEL_ERROR_DEG, abs=0.5)


def test_a_target_off_the_look_direction_keeps_the_ideal_look(h):
    """Pointed away from the target, MVDR must not steer back onto it."""
    _set(h, mode="Signal vs Time", hold_angle=0.0, sim_target_angle_deg=15.0)
    h.sdr.channel_phase_error_deg = 0.0
    data = h.sdr.rx()
    look = h._mvdr_look_vector(data[0], data[1])
    assert np.allclose(look, [1, 1])


def test_noise_alone_keeps_the_ideal_look(h):
    rng = np.random.default_rng(1)
    n0 = rng.normal(size=4096) + 1j * rng.normal(size=4096)
    n1 = rng.normal(size=4096) + 1j * rng.normal(size=4096)
    assert np.allclose(h._mvdr_look_vector(n0, n1), [1, 1])
