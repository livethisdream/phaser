"""The synthetic interferer do_sweep() injects into the received IQ.

A second HB100 as a jammer is not something a lab can reliably set up, so the
MVDR lab computes the jammer instead and adds it to the real IQ, before the
digital weights. These tests pin what the lab relies on: that the injected
signal is the one a real array would receive, that it shows up in the beam
pattern, that MVDR suppresses it, and that it cannot drag the target-tone
tracker it is placed relative to.
"""

import contextlib
import io
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

np = pytest.importorskip("numpy")


def _headless(patch=None):
    sys.modules.setdefault("adi", types.ModuleType("adi"))
    try:
        import phaser_headless
    except Exception as exc:  # noqa: BLE001 - zmq/msgpack absent
        pytest.skip(f"phaser_headless unavailable: {exc}")

    with contextlib.redirect_stdout(io.StringIO()):
        h = phaser_headless.PhaserHeadless.__new__(phaser_headless.PhaserHeadless)
        h.sim_mode = True
        h.c = 299792458
        h._do_init_hardware()
        if patch:
            h.handle_command({"cmd": "set_state", "data": {"state": patch}})
    return h


def _sweeps(h, n):
    for _ in range(n):
        # Hold modes throttle the spectrum; the tests want it every time.
        h._last_fft_t = None
        out = h.do_sweep()
    return out


def _tone_dbfs(result, freq_hz):
    xf = np.asarray(result["xf"])
    g = np.asarray(result["max_gain"])
    i = int(np.argmin(np.abs(xf - freq_hz)))
    return float(g[max(0, i - 3):i + 4].max())


@pytest.mark.parametrize("angle", [-40.0, 0.0, 17.0, 55.0])
@pytest.mark.parametrize("ph_delta", [-90.0, 0.0, 33.75])
def test_injected_wave_matches_the_simulators_plane_wave(angle, ph_delta, monkeypatch):
    """Two independent implementations of 'a plane wave through the ADAR'.

    SimSDR synthesizes its target from the latched ADAR registers; the
    interferer is computed from the commanded values. Put the simulated target
    where the interferer is, at the same level and tone, and the two must be
    the same signal -- or the jammer MVDR is nulling is not one the array
    could have received.
    """
    import phaser_sim
    from ADAR_pyadi_functions import ADAR_set_Phase

    monkeypatch.setattr(phaser_sim.SimSDR, "NOISE_SIGMA", 0.0)
    monkeypatch.setattr(phaser_sim.SimSDR, "TARGET_ANGLE_DEG", angle)

    h = _headless({
        "interferer_enable": True,
        "interferer_angle_deg": angle,
        # SimSDR's target: AMP_SCALE per element, eight elements, full taper.
        "interferer_power_dbfs": 20 * np.log10(8 * phaser_sim.SimSDR.AMP_SCALE / 2**11),
        "interferer_offset_hz": 0.0,
        "interferer_phase_deg": 0.0,
        # Values whose 0-127 register scaling is exact, so the latched taper
        # SimSDR reads is the commanded one.
        "gainList": [0, 100, 100, 100, 100, 100, 100, 0],
        "phaseList": [0, 15, 30, 45, 60, 75, 90, 105],
    })
    h.target_tone_hz = phaser_sim.SimSDR.TARGET_IF_HZ

    ADAR_set_Phase(h.array, ph_delta, h.phase_step, h._apply_phase_cal(h.phaseList))
    sim0, sim1 = h.sdr.rx()
    inj0, inj1 = h._interferer_channels(ph_delta, len(sim0))

    scale = 8 * phaser_sim.SimSDR.AMP_SCALE
    # complex64 synthesis on the simulator side sets the floor.
    assert np.max(np.abs(inj0 - sim0)) / scale < 1e-5
    assert np.max(np.abs(inj1 - sim1)) / scale < 1e-5


def test_interferer_shows_as_a_lobe_in_the_manual_beam_pattern():
    angle = -20.0
    base = _sweeps(_headless(), 2)
    jammed = _sweeps(_headless({
        "interferer_enable": True,
        "interferer_angle_deg": angle,
        "interferer_power_dbfs": -12.0,
    }), 2)

    angles = np.asarray(base["ArrayAngle"])
    i = int(np.argmin(np.abs(angles - angle)))
    assert jammed["ArrayGain"][i] > base["ArrayGain"][i] + 10.0, (
        "the interferer did not appear in the beam pattern at its angle"
    )


def test_mvdr_suppresses_the_interferer_on_a_held_beam():
    """The lab's headline: beam held on the target, jammer in a sidelobe.

    Compared as interferer-to-target ratio, the number the lab's readout
    tile shows.
    """
    common = {
        "mode": "Static Phase",
        "hold_angle": 0.0,
        "interferer_enable": True,
        "interferer_angle_deg": -20.0,
        "interferer_power_dbfs": -12.0,
        "interferer_offset_hz": 100e3,
    }
    sir = {}
    for mode in ("manual", "mvdr"):
        r = _sweeps(_headless({**common, "bfMode": mode}), 2)
        sir[mode] = (_tone_dbfs(r, r["interferer_freq_hz"])
                     - _tone_dbfs(r, r["target_tone_hz"]))
    # ~13 dB with the default 128 snapshots; the margin absorbs noise.
    assert sir["mvdr"] < sir["manual"] - 8.0, f"MVDR did not suppress the interferer: {sir}"


def test_mvdr_keeps_the_target_at_manuals_level():
    """MVDR's output is scaled by Nr, so a held beam reads the target at the
    level Manual does. Unscaled, the distortionless weights [0.5, 0.5] put it
    6 dB below Manual's [1, 1], which the 2026-10-25 bench run read as MVDR
    cancelling the target."""
    common = {
        "mode": "Static Phase",
        "hold_angle": 0.0,
        "interferer_enable": True,
        "interferer_angle_deg": 20.0,
        "interferer_power_dbfs": -12.0,
        "interferer_offset_hz": 100e3,
        "mvdrDiagLoad": 0.1,
    }
    level = {}
    for mode in ("manual", "mvdr"):
        r = _sweeps(_headless({**common, "bfMode": mode}), 2)
        level[mode] = _tone_dbfs(r, r["target_tone_hz"])
    assert abs(level["mvdr"] - level["manual"]) < 1.0, f"target level moved: {level}"


def test_a_loud_interferer_cannot_capture_the_tone_tracker():
    """The tracker reads pre-injection IQ. Were it to see the interferer, it
    would lock onto it and walk it offset_hz further every sweep."""
    import phaser_sim

    h = _headless({
        "interferer_enable": True,
        "interferer_angle_deg": 10.0,
        "interferer_power_dbfs": 0.0,
    })
    r = _sweeps(h, 4)
    bin_hz = h.SampleRate / len(r["xf"])
    assert abs(r["target_tone_hz"] - phaser_sim.SimSDR.TARGET_IF_HZ) <= bin_hz
    assert r["interferer_freq_hz"] == pytest.approx(r["target_tone_hz"] + 100e3)


def test_disabled_interferer_leaves_the_sweep_untouched():
    """Configured but off must be bit-identical to never configured."""
    plain = _headless().do_sweep()
    off = _headless({
        "interferer_enable": False,
        "interferer_angle_deg": -33.0,
        "interferer_power_dbfs": -3.0,
        "interferer_offset_hz": 0.0,
    }).do_sweep()
    assert off["ArrayGain"] == plain["ArrayGain"]
    assert off["ArrayDelta"] == plain["ArrayDelta"]
    assert off["PhaseDiff"] == plain["PhaseDiff"]
    assert off["interferer_freq_hz"] is None


def test_set_state_clamps_and_get_state_echoes():
    h = _headless({
        "interferer_enable": True,
        "interferer_angle_deg": 120.0,
        "interferer_power_dbfs": 9.0,
        "interferer_offset_hz": 5e6,
        "interferer_phase_deg": -90.0,
    })
    # Normally set by __init__, which _headless() skips.
    h.sweeping = False
    h._shutdown_permitted = False
    with contextlib.redirect_stdout(io.StringIO()):
        data = h.handle_command({"cmd": "get_state"})["data"]
    assert data["interferer_enable"] is True
    assert data["interferer_angle_deg"] == 90.0
    assert data["interferer_power_dbfs"] == 0.0
    assert data["interferer_offset_hz"] == h.SampleRate / 4
    assert data["interferer_phase_deg"] == 270.0


def _held_mvdr(angle, **extra):
    return _sweeps(_headless({
        "bfMode": "mvdr",
        "mode": "Static Phase",
        "hold_angle": 0.0,
        "interferer_enable": True,
        "interferer_angle_deg": angle,
        "interferer_power_dbfs": -6.0,
        **extra,
    }), 2)


@pytest.mark.parametrize("angle", [-40.0, -20.0, 25.0])
def test_adaptive_pattern_nulls_the_interferer(angle):
    """The picture step 3 exists for: a notch where the jammer is, unity
    where the beam looks, and the conventional pattern beside it showing the
    jammer was sitting in a sidelobe."""
    pat = _held_mvdr(angle)["adaptive_pattern"]
    angles = np.asarray(pat["angles"])
    adaptive = np.asarray(pat["adaptive_db"])
    conventional = np.asarray(pat["conventional_db"])

    look = int(np.argmin(np.abs(angles - 0.0)))
    jam = int(np.argmin(np.abs(angles - angle)))
    assert abs(adaptive[look]) < 1.5, "MVDR's distortionless response is off at the look angle"
    assert abs(conventional[look]) < 0.1
    # The null can sit a degree or two off: noise in the 128-snapshot
    # covariance estimate. Its depth is set by the jammer's power over the
    # noise and the diagonal load; ~10-20 dB below the sidelobe it sat in at
    # this level. (Not at every angle: where the conventional pattern already
    # has a null, MVDR has nothing to gain and can read worse.)
    near = np.abs(angles - angle) <= 3.0
    assert adaptive[near].min() < conventional[jam] - 10.0, (
        f"no notch at {angle} deg: adaptive {adaptive[near].min():.1f} dB vs "
        f"conventional {conventional[jam]:.1f} dB"
    )


def test_adaptive_pattern_only_on_a_held_mvdr_beam():
    """A sweep re-solves the weights at every angle, so there is no one
    pattern to draw; manual mode has no adaptive weights at all."""
    sweep = _sweeps(_headless({"bfMode": "mvdr", "interferer_enable": True}), 1)
    manual = _sweeps(_headless({"mode": "Static Phase", "interferer_enable": True}), 1)
    assert "adaptive_pattern" not in sweep
    assert "adaptive_pattern" not in manual


def test_interferer_and_mvdr_settings_survive_a_hardware_reinit():
    """Find HB100 and Calibrate re-run _do_init_hardware. The operator's
    interferer and MVDR settings must come through it; they used to reset to
    -20 deg / -10 dBFS and the default loading."""
    h = _headless({
        "bfMode": "mvdr",
        "mvdrDiagLoad": 0.1,
        "interferer_enable": True,
        "interferer_angle_deg": 20.0,
        "interferer_power_dbfs": 0.0,
    })
    with contextlib.redirect_stdout(io.StringIO()):
        h._do_init_hardware()
    assert h.bf_mode == "mvdr"
    assert h.mvdr_diag_load == 0.1
    assert h.interferer_enable is True
    assert h.interferer_angle_deg == 20.0
    assert h.interferer_power_dbfs == 0.0


def test_readout_frequency_is_the_one_injected_when_the_target_drifts():
    """The interferer goes in off the tone measured on the previous sweep.
    If the HB100 drifted since, the payload must still name where the
    interferer actually is, or the readout reads the noise floor."""
    h = _headless({
        "mode": "Static Phase",
        "hold_angle": 0.0,
        "interferer_enable": True,
        "interferer_angle_deg": 20.0,
        "interferer_offset_hz": 100e3,
    })
    measured = _sweeps(h, 1)["target_tone_hz"]
    h.target_tone_hz = measured + 5e3   # as if the HB100 moved 5 kHz
    h._last_fft_t = None
    r = h.do_sweep()
    assert r["interferer_freq_hz"] == pytest.approx(measured + 5e3 + 100e3)
    # And the spectrum has the interferer there, well above the floor.
    assert _tone_dbfs(r, r["interferer_freq_hz"]) > _tone_dbfs(r, measured + 100e3) + 20
