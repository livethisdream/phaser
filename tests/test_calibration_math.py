"""The restored array-calibration procedures, against a simulated 8-element array.

These drive real hardware, so they are exercised here with a fake phaser whose
element responses are known -- which means the assertions can be about whether
the algorithm actually recovers the mismatch it is supposed to, not merely that
it runs.
"""

import numpy as np
import pytest

from phaser_functions import (
    channel_calibration, gain_calibration, phase_calibration, spec_est,
)


class FakeSDR:
    """rx() lives here, not on the array -- _rx_channels calls phaser.sdr.rx()."""

    sample_rate = 30_000_000

    def __init__(self, array):
        self.array = array

    def rx(self):
        return self.array._capture()


class FakeArray:
    """An 8-element array with a known per-element gain and phase error.

    rx() returns the coherent sum of whatever elements are switched on, each
    scaled by its true gain and rotated by its true phase error plus whatever
    trim the caller has applied. A correct calibration recovers the inverse.
    """

    phase_step_size = 15

    def __init__(self, true_gains, true_phases_deg, ch_imbalance_db=0.0, n=256,
                 noise=0.0, seed=1):
        self.sdr = FakeSDR(self)
        # `noise` is complex Gaussian added to the captured samples, as a
        # fraction of the per-element amplitude. Zero by default so every
        # existing test is unaffected; the point of having it is that a
        # noiseless bench cannot distinguish a good phase estimator from a
        # bad one -- both land within a step.
        self.noise = float(noise)
        self.rng = np.random.default_rng(seed)
        self.true_gains = np.asarray(true_gains, dtype=float)
        self.true_phases = np.deg2rad(np.asarray(true_phases_deg, dtype=float))
        self.ch_imbalance = 10 ** (ch_imbalance_db / 20)
        self.n = n
        self.chan_gain = np.zeros(8)
        self.chan_phase = np.zeros(8)
        self.ccal = self.gcal = self.pcal = None

    def set_chan_gain(self, chan, gain, apply_cal=False):
        self.chan_gain[chan] = gain / 127.0

    def set_chan_phase(self, chan, phase_deg, apply_cal=False):
        self.chan_phase[chan] = np.deg2rad(phase_deg)

    def _capture(self):
        t = np.arange(self.n) / self.sdr.sample_rate
        tone = np.exp(2j * np.pi * 1e6 * t)
        weights = self.chan_gain * self.true_gains * np.exp(
            1j * (self.true_phases - self.chan_phase))
        field = complex(np.sum(weights))
        sig = tone * field * 1024
        if self.noise:
            scale = self.noise * 1024
            sig = sig + (self.rng.normal(0, scale, self.n)
                         + 1j * self.rng.normal(0, scale, self.n))
        return np.array([sig, sig * self.ch_imbalance])


def test_channel_calibration_recovers_a_known_imbalance():
    """ch1 is 6 dB down, so the correction should be about +6 dB."""
    arr = FakeArray([1.0] * 8, [0.0] * 8, ch_imbalance_db=-6.0)
    arr.chan_gain[:] = 1.0
    ccal = channel_calibration(arr, averages=4)
    assert ccal[0] == 0.0
    assert ccal[1] == pytest.approx(6.0, abs=0.2)


def test_gain_calibration_trims_strong_elements_toward_the_weakest():
    """Element 3 is 6 dB down, so it becomes the reference at trim 1.0 and the
    rest are pulled down to roughly half amplitude."""
    gains = [1.0] * 8
    gains[3] = 0.5
    arr = FakeArray(gains, [0.0] * 8)
    gain_calibration(arr, averages=2)

    assert len(arr.gcal) == 8
    assert all(0.0 < g <= 1.0 for g in arr.gcal), "a trim may never exceed unity"
    assert arr.gcal[3] == pytest.approx(1.0, abs=0.02), "weakest element is the reference"
    for i in (0, 1, 2, 4, 5, 6, 7):
        assert arr.gcal[i] == pytest.approx(0.5, abs=0.05)


def test_phase_calibration_recovers_a_known_progression():
    """A 30 deg step per element must come back as a 30 deg accumulation."""
    true = [0, 30, 60, 90, 120, 150, 180, 210]
    arr = FakeArray([1.0] * 8, true)
    phases, plots = phase_calibration(arr, averages=1)

    assert len(arr.pcal) == 8 and arr.pcal[0] == 0.0
    assert len(plots) == 7, "one response curve per adjacent pair"
    step = arr.phase_step_size
    for i in range(1, 8):
        # pcal accumulates along the array, so compare against the true offset
        # wrapped into the swept range, within one step of resolution.
        want = ((true[i] + 180) % 360) - 180
        got = ((arr.pcal[i] + 180) % 360) - 180
        assert min(abs(got - want), 360 - abs(got - want)) <= step, (
            f"element {i}: recovered {got} deg, expected about {want} deg")


def test_calibration_survives_a_dead_channel():
    """A silent element must not produce inf/nan and poison every other trim."""
    gains = [1.0] * 8
    gains[5] = 0.0
    arr = FakeArray(gains, [0.0] * 8)
    gain_calibration(arr, averages=1)
    assert all(np.isfinite(g) for g in arr.gcal)


def test_phase_calibration_holds_up_under_noise():
    """The case that matters on a bench, and the one a noiseless fake hides.

    Five real runs on kit phaser-00 scattered by 27.5 degrees averaged over
    elements 2-8, reaching 104 degrees of spread on element 8 -- because the
    old estimator took the argmax of a response whose peak is quadratic, and
    each pair's error seeds the next. Here the same procedure runs eight
    times on noisy captures and has to land on the true progression.
    """
    true = [0, 30, 60, 90, 120, 150, 180, 210]
    recovered = []
    for seed in range(8):
        arr = FakeArray([1.0] * 8, true, noise=0.03, seed=seed)
        phase_calibration(arr, averages=1)
        recovered.append(list(arr.pcal))

    recovered = np.array(recovered)
    wrap = lambda d: (d + 180) % 360 - 180

    def circ_mean(deg):
        """An ordinary mean is wrong on angles: element 7 sits at 180 deg,
        where +179 and -179 average to 0 rather than to 180."""
        return np.degrees(np.angle(np.mean(np.exp(1j * np.radians(deg)))))

    # Run-to-run scatter: the quantity that was 27.5 degrees on hardware.
    scatter = np.array([
        wrap(recovered[:, i] - circ_mean(recovered[:, i])).std(ddof=1)
        for i in range(1, 8)
    ]).mean()
    assert scatter < 5.0, f"run-to-run scatter {scatter:.1f} deg"

    # And it has to be scattered around the RIGHT answer, not just a
    # consistent wrong one -- a stable estimator that is biased would pass
    # the check above and still miscalibrate the array.
    for i in range(1, 8):
        err = abs(wrap(circ_mean(recovered[:, i]) - true[i]))
        assert err < 8.0, (
            f"element {i}: recovered {circ_mean(recovered[:, i]):.1f} deg, "
            f"expected {wrap(true[i]):.1f}, off by {err:.1f}")
