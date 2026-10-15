"""The HB100 search must measure the LO it just set, at the rate it runs at.

Found on a kit on 2026-10-15. `_capture` discarded one RX buffer after each
retune, but every capture still held the previous LO's data, whatever the
settle time, so each 10 MHz sweep step was labelled with the next step's
frequency and the search landed ~8 MHz off a real 74 dB tone. Separately,
loading LTE20_MHz.ftr moves the radio to 30.72 MSPS while the frequency axis
assumed 30 MSPS. Destroying the buffer and using the radio's own sample rate
measured the tone within one bin from any jump.
"""

import sys
import types

import pytest

np = pytest.importorskip("numpy")

TONE_HZ = 10.4077e9
FS = 30_720_000          # what the radio reports once LTE20_MHz.ftr is loaded
NBUF = 1024
BIN_HZ = FS / NBUF


@pytest.fixture(scope="module")
def hb100():
    adi = sys.modules.setdefault("adi", types.ModuleType("adi"))
    if not hasattr(adi, "ad9361"):
        adi.ad9361 = type("ad9361", (), {})
    cn0566 = sys.modules.setdefault("adi.cn0566", types.ModuleType("adi.cn0566"))
    if not hasattr(cn0566, "CN0566"):
        cn0566.CN0566 = type("CN0566", (), {})
    adi.cn0566 = cn0566
    import phaser_find_hb100_headless as mod
    mod.sleep = lambda _s: None
    return mod


class FakeSdr:
    """A receiver whose buffer keeps the LO it was created under.

    Reads without destroying the buffer return data from that LO, which is
    how the hardware behaved: one or two discarded reads did not help.
    """

    rx_lo = int(2.0e9)
    sample_rate = FS

    def __init__(self, phaser):
        self.phaser = phaser
        self.buffer_centre = None

    def rx_destroy_buffer(self):
        self.buffer_centre = None

    def rx(self):
        if self.buffer_centre is None:
            self.buffer_centre = self.phaser.centre_hz()
        offset = TONE_HZ - self.buffer_centre
        # _capture flips the spectrum (high-side LO), so the tone arrives
        # mirrored at baseband.
        t = np.arange(NBUF) / FS
        tone = 1000 * np.exp(-2j * np.pi * offset * t)
        return [tone, tone]


class FakePhaser:
    SignalFreq = 0.0
    frequency = 0

    def centre_hz(self):
        return self.frequency * 4 - self.sdr.rx_lo


def absolute_peak(mod, phaser, sdr, centre_hz):
    ampl, bb = mod._capture(phaser, sdr, centre_hz)
    return centre_hz + bb[int(np.argmax(ampl))]


@pytest.mark.parametrize("jump_mhz", [-300, -10, +10, +300])
@pytest.mark.parametrize("shift_mhz", [-4, +4])
def test_capture_reads_the_new_lo_after_a_jump(hb100, jump_mhz, shift_mhz):
    phaser = FakePhaser()
    sdr = FakeSdr(phaser)
    phaser.sdr = sdr
    hb100._capture(phaser, sdr, TONE_HZ + jump_mhz * 1e6)
    found = absolute_peak(hb100, phaser, sdr, TONE_HZ + shift_mhz * 1e6)
    assert abs(found - TONE_HZ) <= 2 * BIN_HZ, (
        f"tone measured {(found - TONE_HZ) / 1e3:+.1f} kHz off after a "
        f"{jump_mhz:+d} MHz jump"
    )


def test_axis_uses_the_radio_sample_rate(hb100):
    """At 30.72 MSPS a 30 MSPS axis puts a 4 MHz offset ~96 kHz short."""
    phaser = FakePhaser()
    sdr = FakeSdr(phaser)
    phaser.sdr = sdr
    found = absolute_peak(hb100, phaser, sdr, TONE_HZ + 4e6)
    assert abs(found - TONE_HZ) <= 2 * BIN_HZ
