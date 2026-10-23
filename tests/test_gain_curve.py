"""The rx_gain curve measurement's analysis, without hardware.

phaser_gain_curve_headless.py runs on the Pi against a real HB100; what can be
checked here is that its arithmetic recovers a known curve and phase, and that
the table it emits is one rx_gain_code() can invert.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

np = pytest.importorskip("numpy")

import phaser_gain_curve_headless as gc  # noqa: E402


def test_tone_at_peak_recovers_relative_phase_and_level():
    n = 4096
    t = np.arange(n)
    tone = np.exp(2j * np.pi * (410 / n) * t)  # on a bin: no scalloping
    ref = 1000 * tone
    test = 500 * tone * np.exp(1j * np.radians(37.0))

    mag, phase, dbfs = gc.tone_at_peak(test, ref)
    assert phase == pytest.approx(37.0, abs=1e-6)
    assert mag == pytest.approx(500, rel=1e-3)
    assert dbfs == pytest.approx(20 * np.log10(500 / 2**11), abs=0.01)


def test_fit_recovers_the_exponent_and_ignores_the_floor():
    codes = list(range(128))
    floor = 0.02
    # The power law, plus a leak that dominates the bottom codes.
    amps = [max((c / 127) ** 0.68, floor) for c in codes]
    p, used = gc.fit_exponent(codes, amps, floor)
    assert p == pytest.approx(0.68, abs=1e-6)
    assert 0 < used < 126


def test_table_is_invertible_by_rx_gain_code(monkeypatch):
    import ADAR_pyadi_functions as adar

    curves = [
        {c: (c / 127) ** 0.66 for c in range(0, 128, 4)} | {127: 1.0},
        {c: (c / 127) ** 0.70 for c in range(0, 128, 4)} | {127: 1.0},
    ]
    table = gc.amplitude_table(curves)

    assert len(table) == 128
    assert table[0] == 0.0 and table[127] == 1.0
    assert all(b >= a for a, b in zip(table, table[1:]))

    monkeypatch.setattr(adar, "RX_GAIN_AMPLITUDE", table)
    for pct in (10, 25, 50, 75, 100):
        code = adar.rx_gain_code(pct / 100)
        assert abs(table[code] - pct / 100) < 0.02
