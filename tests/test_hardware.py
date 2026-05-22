"""
Hardware verification tests for the ADI Phaser device.

These tests are skipped automatically unless a live device is reachable.
Run with:
    uv run python -m pytest tests/test_hardware.py -m hardware -v
Or via the convenience script:
    .\\scripts\\test.ps1 -Markers hardware -HardwareUri "ip:phaser.local:ip:phaser.local:50901"

The PHASER_RPI_URI and PHASER_SDR_URI environment variables are used for address
overrides, matching the same precedence logic as the production server.
"""

import os
import time

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Hardware availability probe — gates every test in this module
# ---------------------------------------------------------------------------

def _probe_rpi(rpi_uri: str) -> bool:
    """Return True if the RPi GPIO bridge responds."""
    try:
        import adi
        dev = adi.one_bit_adc_dac(rpi_uri)
        _ = dev.gpio_vctrl_1
        return True
    except Exception:
        return False


def _probe_sdr(sdr_uri: str) -> bool:
    """Return True if the PlutoSDR responds."""
    try:
        import adi
        sdr = adi.Pluto(sdr_uri)
        _ = sdr.sample_rate
        return True
    except Exception:
        return False


def _resolve_test_uris():
    """Return (rpi_uri, sdr_uri) using the same precedence as the server."""
    rpi = os.environ.get("PHASER_RPI_URI", "ip:phaser.local")
    sdr = os.environ.get("PHASER_SDR_URI", "ip:phaser.local:50901")
    return rpi, sdr


# Module-level availability check — evaluated once at collection time.
_RPI_URI, _SDR_URI = _resolve_test_uris()
_HW_AVAILABLE = _probe_rpi(_RPI_URI)
_SDR_AVAILABLE = _probe_sdr(_SDR_URI)

hardware = pytest.mark.skipif(
    not _HW_AVAILABLE,
    reason=f"Phaser hardware not reachable at {_RPI_URI} — set PHASER_RPI_URI if using a custom address",
)
hardware_sdr = pytest.mark.skipif(
    not (_HW_AVAILABLE and _SDR_AVAILABLE),
    reason="Full hardware stack (RPi + SDR) not available",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def phaser_server():
    """
    Start a real PhaserServer connected to live hardware.
    Yielded object is the PhaserServer instance; shut down after module tests.
    """
    from phaser_service import PhaserServer
    server = PhaserServer()
    yield server
    server.shutdown()


@pytest.fixture(scope="module")
def gpio(phaser_server):
    return phaser_server.gpios


@pytest.fixture(scope="module")
def sdr(phaser_server):
    return phaser_server.sdr


@pytest.fixture(scope="module")
def array(phaser_server):
    return phaser_server.array


# ---------------------------------------------------------------------------
# T1 — Connectivity
# ---------------------------------------------------------------------------

@hardware
@pytest.mark.hardware
class TestConnectivity:
    """Verify each hardware subsystem is reachable and responds."""

    def test_gpio_bridge_reachable(self):
        """One-bit ADC/DAC GPIO bridge on the RPi is accessible."""
        import adi
        dev = adi.one_bit_adc_dac(_RPI_URI)
        # Reading any GPIO attribute without exception is a pass.
        _ = dev.gpio_vctrl_1
        assert True

    def test_sdr_reachable(self):
        """PlutoSDR is accessible and reports a valid sample rate."""
        import adi
        sdr = adi.Pluto(_SDR_URI)
        assert sdr.sample_rate > 0

    def test_lo_reachable(self, phaser_server):
        """ADF4159 LO synthesiser on the RPi is accessible.

        Uses phaser_server.lo (the already-open synth object) to avoid a
        redundant mDNS lookup that can fail on Windows after multiple context opens.
        """
        lo = phaser_server.lo
        if lo is None:
            pytest.skip("LO object not available — SDR_LO_init returned None")
        assert lo.frequency > 0

    def test_adar_array_reachable(self, array):
        """ADAR1000 array is accessible and has both chips.

        Uses the shared phaser_server fixture's already-open array object to
        avoid a redundant mDNS lookup on Windows that fails after 3+ concurrent
        iio context opens to the same phaser.local address.
        """
        assert len(array.devices) == 2


# ---------------------------------------------------------------------------
# T2 — GPIO / Transmit control
# ---------------------------------------------------------------------------

@hardware
@pytest.mark.hardware
class TestGPIOControl:
    """Verify GPIO signal routing behaves as specified."""

    def test_vctrl_default_state(self, gpio):
        """Voltage control lines should be high by default after init."""
        assert gpio.gpio_vctrl_1 == 1
        assert gpio.gpio_vctrl_2 == 1

    def test_tx_switch_toggle(self, gpio):
        """Tx switch can be toggled without error."""
        original = gpio.gpio_tx_sw
        gpio.gpio_tx_sw = 1 - original   # flip
        time.sleep(0.05)
        assert gpio.gpio_tx_sw == (1 - original)
        gpio.gpio_tx_sw = original        # restore
        assert gpio.gpio_tx_sw == original

    def test_vctrl2_toggle(self, gpio):
        """Vctrl2 (PA enable) can be toggled and read back."""
        gpio.gpio_vctrl_2 = 0
        time.sleep(0.05)
        assert gpio.gpio_vctrl_2 == 0
        gpio.gpio_vctrl_2 = 1
        assert gpio.gpio_vctrl_2 == 1


# ---------------------------------------------------------------------------
# T3 — SDR / PlutoSDR
# ---------------------------------------------------------------------------

@hardware_sdr
@pytest.mark.hardware
class TestSDR:
    """Verify PlutoSDR data capture and gain control."""

    def test_sdr_sample_rate(self, sdr):
        """SDR sample rate matches the configured value."""
        from config import SampleRate
        # Allow ±1 ppm tolerance for integer rounding.
        assert abs(sdr.sample_rate - int(SampleRate)) <= 1

    def test_sdr_rx_capture(self, sdr):
        """SDR produces two IQ buffers of the correct length."""
        from SDR_functions import SDR_getData
        data = SDR_getData(sdr)
        assert len(data) == 2
        assert len(data[0]) == sdr.rx_buffer_size
        assert len(data[1]) == sdr.rx_buffer_size

    def test_sdr_rx_not_all_zeros(self, sdr):
        """At least one RX channel should contain non-zero samples (noise floor)."""
        from SDR_functions import SDR_getData
        data = SDR_getData(sdr)
        ch0_power = float(np.mean(np.abs(data[0]) ** 2))
        ch1_power = float(np.mean(np.abs(data[1]) ** 2))
        # Noise floor should exceed 0 unless hardware is dead.
        assert ch0_power > 0 or ch1_power > 0

    def test_sdr_rx_gain_write(self, sdr):
        """Rx gain can be written and reads back within ±1 dB."""
        from SDR_functions import SDR_setRx
        target_gain = 20
        SDR_setRx(sdr, target_gain, target_gain)
        readback = sdr.rx_hardwaregain_chan0
        assert abs(readback - target_gain) <= 1

    def test_sdr_lo_frequency(self, sdr):
        """SDR LO frequency is plausible (> 2 GHz)."""
        assert sdr.rx_lo > 2e9


# ---------------------------------------------------------------------------
# T4 — ADAR / Beamformer
# ---------------------------------------------------------------------------

@hardware
@pytest.mark.hardware
class TestADAR:
    """Verify ADAR1000 phase and gain programming."""

    def test_adar_set_uniform_taper(self, array):
        """Setting all elements to gain=100 completes without error."""
        from ADAR_pyadi_functions import ADAR_set_Taper
        ADAR_set_Taper(array, [100] * 8)

    def test_adar_set_zero_taper(self, array):
        """Setting all elements to gain=0 (mute) completes without error."""
        from ADAR_pyadi_functions import ADAR_set_Taper
        ADAR_set_Taper(array, [0] * 8)
        # Restore
        ADAR_set_Taper(array, [100] * 8)

    def test_adar_set_phase_broadside(self, array):
        """Programming zero phase on all elements (broadside) completes without error."""
        from ADAR_pyadi_functions import ADAR_set_Phase
        ADAR_set_Phase(array, PhDelta=0, phase_step_size=2.8125, phaseList=[0.0] * 8)

    def test_adar_set_phase_steered(self, array):
        """Programming a nonzero phase delta (steered beam) completes without error."""
        from ADAR_pyadi_functions import ADAR_set_Phase
        ADAR_set_Phase(array, PhDelta=45.0, phase_step_size=2.8125, phaseList=[0.0] * 8)

    def test_adar_phase_wraps_correctly(self, array):
        """Phase values above 360° wrap without raising."""
        from ADAR_pyadi_functions import ADAR_set_Phase
        ADAR_set_Phase(array, PhDelta=400.0, phase_step_size=2.8125, phaseList=[0.0] * 8)

    def test_adar_element_readback(self, array):
        """All 8 element rx_phase values are readable after programming."""
        from ADAR_pyadi_functions import ADAR_set_Phase
        ADAR_set_Phase(array, PhDelta=0, phase_step_size=2.8125, phaseList=[0.0] * 8)
        for elem_id in range(1, 9):
            phase = array.elements[elem_id].rx_phase
            assert 0.0 <= phase < 360.0


# ---------------------------------------------------------------------------
# T5 — LO / ADF4159
# ---------------------------------------------------------------------------

@hardware
@pytest.mark.hardware
class TestLO:
    """Verify ADF4159 LO programming."""

    def test_lo_set_frequency(self, phaser_server):
        """LO frequency can be set to a valid BW-plan value and reads back within 1 kHz.

        Uses the PhaserServer's live LO object (self.lo) to avoid a second
        mDNS/DNS lookup on Windows which can fail intermittently.
        """
        lo = phaser_server.lo
        if lo is None:
            pytest.skip("LO object not available on this hardware configuration")
        target = 12_725_000_000   # 12.725 GHz (SignalFreq + Rx_freq nominal)
        lo.frequency = target
        time.sleep(0.1)
        readback = lo.frequency
        assert abs(readback - target) < 1_000



# ---------------------------------------------------------------------------
# T6 — End-to-end sweep (PhaserServer)
# ---------------------------------------------------------------------------

@hardware_sdr
@pytest.mark.hardware
@pytest.mark.slow
class TestEndToEndSweep:
    """
    Exercise the full sweep pipeline against real hardware.
    These are the closest tests to a live integration run.
    """

    def test_single_phase_point(self, phaser_server):
        """A single-point sweep at broadside (PhDelta=0) succeeds."""
        state = {
            "mode": "Beam Sweep",
            "PhaseValues": [0.0],
            "phaseList": [0.0] * 8,
            "gainList": [100] * 8,
            "BW": 10,
            "bits": 7,
            "B0_Gain": 1.0,
            "B1_Gain": 1.0,
            "Beam0_Phase": 0,
            "Beam1_Phase": 0,
            "Averages": 1,
            "Tx_mode": "Transmit Disabled",
        }
        result = phaser_server.process_sweep(state)

        assert len(result["ArrayGain"]) == 1
        assert len(result["ArrayAngle"]) == 1
        # FFT length equals the SDR buffer size — do not hardcode 1024.
        buf = phaser_server.sdr.rx_buffer_size
        assert len(result["max_gain"]) == buf
        assert len(result["xf"]) == buf

    def test_response_keys_present(self, phaser_server):
        """Sweep result contains all keys required by the WebSocket contract."""
        state = {
            "mode": "Beam Sweep",
            "PhaseValues": [-45.0, 0.0, 45.0],
            "phaseList": [0.0] * 8,
            "gainList": [100] * 8,
            "BW": 10,
            "bits": 7,
            "B0_Gain": 1.0,
            "B1_Gain": 1.0,
            "Beam0_Phase": 0,
            "Beam1_Phase": 0,
            "Averages": 1,
            "Tx_mode": "Transmit Disabled",
        }
        result = phaser_server.process_sweep(state)
        for key in ("ArrayGain", "ArrayDelta", "ArrayBeamPhase", "ArrayAngle", "ArrayError", "max_gain", "xf"):
            assert key in result, f"Missing key: {key}"

    def test_gain_values_are_finite(self, phaser_server):
        """All returned gain values must be finite (no NaN/Inf)."""
        state = {
            "mode": "Beam Sweep",
            "PhaseValues": [-30.0, -15.0, 0.0, 15.0, 30.0],
            "phaseList": [0.0] * 8,
            "gainList": [100] * 8,
            "BW": 10,
            "bits": 7,
            "B0_Gain": 1.0,
            "B1_Gain": 1.0,
            "Beam0_Phase": 0,
            "Beam1_Phase": 0,
            "Averages": 1,
            "Tx_mode": "Transmit Disabled",
        }
        result = phaser_server.process_sweep(state)

        gain_arr = np.array(result["ArrayGain"])
        fft_arr = np.array(result["max_gain"])
        assert np.all(np.isfinite(gain_arr)), "ArrayGain contains NaN or Inf"
        assert np.all(np.isfinite(fft_arr)), "max_gain FFT contains NaN or Inf"

    def test_gain_range_plausible(self, phaser_server):
        """Peak array gain should be within a plausible dBFS window (-120 .. 0)."""
        state = {
            "mode": "Beam Sweep",
            "PhaseValues": list(np.arange(-60, 61, 15.0)),
            "phaseList": [0.0] * 8,
            "gainList": [100] * 8,
            "BW": 10,
            "bits": 7,
            "B0_Gain": 1.0,
            "B1_Gain": 1.0,
            "Beam0_Phase": 0,
            "Beam1_Phase": 0,
            "Averages": 2,
            "Tx_mode": "Transmit Disabled",
        }
        result = phaser_server.process_sweep(state)
        peak = max(result["ArrayGain"])
        assert -120 <= peak <= 0, f"Peak gain {peak:.1f} dBFS outside [-120, 0] window"

    def test_static_phase_mode(self, phaser_server):
        """Static Phase mode returns a single data point."""
        state = {
            "mode": "Static Phase",
            "PhaseValues": [],
            "phaseList": [0.0] * 8,
            "gainList": [100] * 8,
            "BW": 10,
            "bits": 7,
            "B0_Gain": 1.0,
            "B1_Gain": 1.0,
            "Beam0_Phase": 0,
            "Beam1_Phase": 0,
            "Averages": 1,
            "Tx_mode": "Transmit Disabled",
        }
        result = phaser_server.process_sweep(state)
        assert len(result["ArrayGain"]) >= 1

    def test_taper_affects_gain(self, phaser_server):
        """Switching from uniform to zeroed taper should change the captured power.

        Requires an active RF signal (HB100 transmitting).  If both measurements
        land near the thermal noise floor the ADAR effect is indistinguishable from
        measurement variance, so the test skips rather than giving a false result.
        """
        base_state = {
            "mode": "Beam Sweep",
            "PhaseValues": [0.0],
            "phaseList": [0.0] * 8,
            "BW": 10,
            "bits": 7,
            "B0_Gain": 1.0,
            "B1_Gain": 1.0,
            "Beam0_Phase": 0,
            "Beam1_Phase": 0,
            "Averages": 8,          # more averages → lower variance at noise floor
            "Tx_mode": "Transmit Disabled",
        }

        state_on = {**base_state, "gainList": [100] * 8}
        state_off = {**base_state, "gainList": [0] * 8}

        result_on = phaser_server.process_sweep(state_on)
        result_off = phaser_server.process_sweep(state_off)

        gain_on = result_on["ArrayGain"][0]
        gain_off = result_off["ArrayGain"][0]

        # If both readings are near thermal noise floor, there is no active signal
        # and the test cannot distinguish ADAR state from measurement noise.
        NOISE_FLOOR_THRESHOLD = -55.0   # dBFS — below this, no HB100 signal present
        MIN_REQUIRED_DELTA    =   2.0   # dB  — minimum meaningful gain reduction

        if gain_on < NOISE_FLOOR_THRESHOLD and gain_off < NOISE_FLOOR_THRESHOLD:
            pytest.skip(
                f"gain_on={gain_on:.1f} dBFS and gain_off={gain_off:.1f} dBFS are both "
                f"below {NOISE_FLOOR_THRESHOLD} dBFS (thermal noise floor). "
                "An active HB100 signal is required to verify the ADAR taper path."
            )

        assert gain_off < gain_on - MIN_REQUIRED_DELTA, (
            f"Expected gain_off ({gain_off:.1f} dBFS) to be at least {MIN_REQUIRED_DELTA} dB "
            f"below gain_on ({gain_on:.1f} dBFS) after zeroing the taper. "
            "If the margin is consistently small, the ADAR may not be in the active RF chain."
        )

