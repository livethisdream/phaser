"""Fitting a two-element phase response instead of taking its argmax.

Found on kit phaser-00, 2026-10-15: phase calibration did not repeat. Five
runs on a healthy bench -- 85 dB SNR, 33 dB array contrast -- returned element
8 offsets spanning 104 degrees (+8.4, +30.9, +50.6, +95.6, +112.5), and 27.5
degrees of run-to-run scatter averaged over elements 2-8, growing along the
array from 17.3 at element 2 to 43.6 at element 8.

Nothing was wrong with the measurement. Two elements summing give a response
proportional to |1 + exp(j*delta)|, whose peak is quadratic: 20 degrees off
costs 0.26 dB. An argmax over that is reading the flattest part of the curve,
so a few tenths of a dB of noise moves it tens of degrees -- and since each
pair's answer is held while the next pair sweeps, the error accumulates.

The fit uses the whole curve: phi0 = angle(sum(P_linear * exp(j*phi))), the
first Fourier coefficient of a known one-parameter family.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import phaser_functions as pf


STEP = 2.8125                       # ADAR1000 phase LSB
PHI = np.arange(-180, 180 + STEP / 2, STEP)


def response_db(offset_deg, phi=PHI, floor_db=-30.0):
    """An ideal two-element sweep, floored where the null meets the noise."""
    amp = np.abs(1 + np.exp(1j * np.radians(phi - offset_deg)))
    return np.maximum(20 * np.log10(np.maximum(amp, 1e-9)), floor_db)


def wrap(deg):
    return (deg + 180) % 360 - 180


# --- the estimator on clean data -------------------------------------------

@pytest.mark.parametrize("offset", [0.0, 37.0, -112.5, 179.0, -179.0])
def test_a_noiseless_sweep_is_recovered_exactly(offset):
    """Not "within a step" -- the fit is continuous, so it beats the grid."""
    got, _ = pf._fit_pair_phase(PHI, response_db(offset))
    assert abs(wrap(got - offset)) < 0.01


def test_the_answer_is_wrapped_into_the_swept_range():
    for offset in (-180.0, 180.0, 270.0):
        got, _ = pf._fit_pair_phase(PHI, response_db(offset))
        assert -180.0 <= got < 180.0


def test_the_duplicated_sweep_endpoint_is_dropped():
    """The sweep is built inclusive of both ends, so -180 and +180 are the
    same electrical phase measured twice."""
    assert PHI[0] == -180.0 and PHI[-1] == 180.0
    with_dup, _ = pf._fit_pair_phase(PHI, response_db(37.0))
    without, _ = pf._fit_pair_phase(PHI[:-1], response_db(37.0, PHI[:-1]))
    assert abs(wrap(with_dup - without)) < 1e-9


def test_quality_is_about_a_third_for_a_clean_response():
    """|c1|/|c0| for a rectified cosine. The number the threshold is set
    against, so it is worth pinning rather than describing."""
    _, quality = pf._fit_pair_phase(PHI, response_db(37.0, floor_db=-200.0))
    assert quality == pytest.approx(1 / 3, abs=0.01)


def test_quality_collapses_on_a_response_carrying_no_phase_information():
    rng = np.random.default_rng(0)
    flat = rng.normal(-40.0, 0.3, PHI.size)
    _, quality = pf._fit_pair_phase(PHI, flat)
    assert quality < pf.MIN_PHASE_FIT_QUALITY


# --- the properties that make it the right estimator -----------------------

def test_an_additive_pedestal_does_not_move_the_fit():
    """The measured peak falls to the noise floor, not to zero. A constant is
    orthogonal to exp(j*phi), so the fit never has to know where the floor
    is -- which is the reason for fitting c1 rather than, say, a centroid."""
    clean = response_db(37.0, floor_db=-200.0)
    linear = 10 ** (clean / 20)
    pedestal = 20 * np.log10(linear + 0.25 * linear.max())
    base, _ = pf._fit_pair_phase(PHI, clean)
    lifted, _ = pf._fit_pair_phase(PHI, pedestal)
    assert abs(wrap(lifted - base)) < 0.2


def test_a_coarse_sweep_still_resolves_far_better_than_its_step():
    """15 deg steps, which is what the fake array in test_calibration_math
    uses and well short of the ADAR's own resolution."""
    coarse = np.arange(-180, 180 + 7.5, 15.0)
    got, _ = pf._fit_pair_phase(coarse, response_db(37.0, coarse))
    assert abs(wrap(got - 37.0)) < 1.0


# --- against the estimator it replaces -------------------------------------

def test_the_fit_beats_the_argmax_by_more_than_an_order_of_magnitude():
    """Calibrated to the bench: ~0.4 dB of noise reproduces the ~17 degrees
    of per-pair argmax scatter actually seen on phaser-00."""
    rng = np.random.default_rng(7)
    fit_err, arg_err = [], []
    for _ in range(300):
        offset = rng.uniform(-180, 180)
        noisy = response_db(offset) + rng.normal(0, 0.4, PHI.size)
        got, _ = pf._fit_pair_phase(PHI, noisy)
        fit_err.append(wrap(got - offset))
        arg_err.append(wrap(PHI[int(np.argmax(noisy))] - offset))

    fit_std = np.std(fit_err)
    arg_std = np.std(arg_err)
    assert arg_std > 10.0, f"argmax should be bad here, got {arg_std:.1f} deg"
    assert fit_std < 2.0, f"fit scatter {fit_std:.1f} deg"
    assert arg_std / fit_std > 10.0


def test_the_fit_degrades_gracefully_as_noise_grows():
    """No cliff: an estimator that is excellent until it suddenly is not
    would be worse on a bench than one that is merely good."""
    rng = np.random.default_rng(11)
    previous = 0.0
    for noise_db in (0.1, 0.4, 1.6, 6.4):
        errs = []
        for _ in range(200):
            offset = rng.uniform(-180, 180)
            noisy = response_db(offset) + rng.normal(0, noise_db, PHI.size)
            got, _ = pf._fit_pair_phase(PHI, noisy)
            errs.append(wrap(got - offset))
        std = np.std(errs)
        assert std > previous or std < 1.0
        assert std < 25.0, f"{noise_db} dB noise -> {std:.1f} deg"
        previous = std
