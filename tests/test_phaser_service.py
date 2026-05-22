"""
Unit and integration tests for phaser_service.py (BackendService, PhaserServerSim).
"""
import json
from unittest.mock import MagicMock

import numpy as np
import pytest

from phaser_service import BackendService, PhaserServerSim, default_serializer


@pytest.mark.unit
class TestPhaserServerSim:
    """Test PhaserServerSim simulation mode (no hardware required)."""

    def test_sim_initialization(self, mock_config):
        """Test PhaserServerSim initializes with default values."""
        sim = PhaserServerSim()

        # SignalFreq may come from persisted HB100 calibration if present.
        assert sim.SignalFreq > 0
        assert sim.Rx_freq == mock_config.Rx_freq
        assert sim.SampleRate == mock_config.SampleRate
        assert sim.Rx_gain == mock_config.Rx_gain
        assert sim.Tx_gain == mock_config.Tx_gain
        assert sim.Averages == mock_config.Averages
        assert sim.d == mock_config.d
        assert sim.Tx_mode == "Transmit Disabled"

    def test_sim_process_sweep_basic(self, mock_config, sample_state):
        """Test sim process_sweep returns correct structure."""
        sim = PhaserServerSim()
        result = sim.process_sweep(sample_state)

        assert isinstance(result, dict)
        assert "ArrayGain" in result
        assert "ArrayDelta" in result
        assert "ArrayBeamPhase" in result
        assert "ArrayAngle" in result
        assert "ArrayError" in result
        assert "max_gain" in result
        assert "xf" in result

    def test_sim_process_sweep_gain_output_shape(self, mock_config, sample_state):
        """Test sim process_sweep output arrays match PhaseValues length."""
        sim = PhaserServerSim()
        result = sim.process_sweep(sample_state)

        phase_values_count = len(sample_state["PhaseValues"])
        assert len(result["ArrayGain"]) == phase_values_count
        assert len(result["ArrayDelta"]) == phase_values_count
        assert len(result["ArrayAngle"]) == phase_values_count
        assert len(result["ArrayError"]) == phase_values_count
        assert len(result["ArrayBeamPhase"]) == phase_values_count

    def test_sim_process_sweep_fft_output(self, mock_config, sample_state):
        """Test sim process_sweep FFT outputs have correct shape."""
        sim = PhaserServerSim()
        result = sim.process_sweep(sample_state)

        assert len(result["max_gain"]) == 1024
        assert len(result["xf"]) == 1024

    def test_sim_process_sweep_updates_state(self, mock_config, sample_state):
        """Test sim process_sweep updates internal state from state dict."""
        sim = PhaserServerSim()

        new_state = sample_state.copy()
        new_state["SignalFreq"] = 11e9
        new_state["Rx_gain"] = 20

        sim.process_sweep(new_state)

        assert sim.SignalFreq == 11e9
        assert sim.Rx_gain == 20

    def test_sim_process_sweep_empty_phase_values(self, mock_config, sample_state):
        """Test sim process_sweep with no phase values."""
        sim = PhaserServerSim()

        state = sample_state.copy()
        state["PhaseValues"] = []
        result = sim.process_sweep(state)

        assert isinstance(result, dict)
        assert len(result["ArrayGain"]) == 0

    def test_sim_process_sweep_signal_vs_time_mode(self, mock_config, sample_state):
        """Test sim process_sweep in 'Signal vs Time' mode."""
        sim = PhaserServerSim()

        state = sample_state.copy()
        state["mode"] = "Signal vs Time"
        result = sim.process_sweep(state)

        # In Signal vs Time mode, gain list should still be populated
        assert len(result["ArrayGain"]) > 0

    def test_sim_default_serializer_with_numpy_types(self):
        """Test default_serializer handles numpy types correctly."""
        data = {
            "array": np.array([1.0, 2.0, 3.0]),
            "float32": np.float32(1.5),
            "float64": np.float64(2.5),
            "int32": np.int32(5),
            "int64": np.int64(10),
            "ndarray": np.ndarray([2, 2]),
        }

        # Should not raise
        json_str = json.dumps(data, default=default_serializer)
        assert isinstance(json_str, str)


@pytest.mark.integration
class TestBackendService:
    """Test BackendService framework wrapper."""

    def test_backend_service_init_sim_mode(self, mock_config):
        """Test BackendService initializes in sim mode."""
        service = BackendService(sim_mode=True)

        assert service.sim_mode is True
        assert service.hardware is None
        assert service.calibration_status["running"] is False

    def test_backend_service_init_real_mode(self, mock_config):
        """Test BackendService initializes in real mode (not started)."""
        service = BackendService(sim_mode=False)

        assert service.sim_mode is False
        assert service.hardware is None

    def test_backend_service_startup_sim(self, mock_config):
        """Test BackendService startup in sim mode."""
        service = BackendService(sim_mode=True)
        service.startup()

        assert service.hardware is not None
        assert isinstance(service.hardware, PhaserServerSim)

    def test_backend_service_shutdown(self, mock_config):
        """Test BackendService shutdown."""
        service = BackendService(sim_mode=True)
        service.startup()
        service.shutdown()

        # Should not raise
        assert service.hardware is not None

    def test_backend_service_process_sweep_before_startup(self, mock_config, sample_state):
        """Test process_sweep raises error before startup."""
        service = BackendService(sim_mode=True)

        with pytest.raises(RuntimeError, match="not initialized"):
            service.process_sweep(sample_state)

    def test_backend_service_process_sweep_after_startup(self, mock_config, sample_state):
        """Test process_sweep works after startup."""
        service = BackendService(sim_mode=True)
        service.startup()

        result = service.process_sweep(sample_state)

        assert isinstance(result, dict)
        assert "ArrayGain" in result

    def test_backend_service_get_ui_state_before_startup(self, mock_config):
        """Test get_ui_state returns defaults before hardware startup."""
        service = BackendService(sim_mode=True)

        state = service.get_ui_state()

        assert state["status"] == "ok"
        assert "data" in state
        assert state["data"]["SignalFreq"] == mock_config.SignalFreq

    def test_backend_service_get_ui_state_after_startup(self, mock_config):
        """Test get_ui_state returns hardware values after startup."""
        service = BackendService(sim_mode=True)
        service.startup()

        state = service.get_ui_state()

        assert state["status"] == "ok"
        assert "data" in state
        assert "SignalFreq" in state["data"]
        assert "Rx_freq" in state["data"]
        assert "Rx_gain" in state["data"]

    def test_reload_runtime_calibration_calls_hardware_hook(self, mock_config):
        """Successful calibration completion reloads live hardware calibration state."""
        service = BackendService(sim_mode=True)
        service.hardware = MagicMock()

        service._reload_runtime_calibration("find_hb100")

        service.hardware.reload_calibration.assert_called_once_with("find_hb100")


@pytest.mark.unit
class TestSweepStateManagement:
    """Test state management and phase/gain calculations."""

    def test_compute_phase_sweep_beam_sweep_mode(self, mock_config, sample_state):
        """Test phase sweep computation in Beam Sweep mode."""
        sim = PhaserServerSim()
        sample_state["mode"] = "Beam Sweep"
        sample_state["PhaseValues"] = [-45, 0, 45]

        result = sim.process_sweep(sample_state)

        assert len(result["ArrayGain"]) == 3
        assert len(result["ArrayAngle"]) == 3

    def test_compute_phase_sweep_static_phase_mode(self, mock_config, sample_state):
        """Test phase sweep computation in Static Phase mode."""
        sim = PhaserServerSim()
        sample_state["mode"] = "Static Phase"
        sample_state["PhaseValues"] = []

        result = sim.process_sweep(sample_state)

        # Static Phase with empty PhaseValues should use single 0 value
        assert len(result["ArrayGain"]) == 1

    def test_process_sweep_with_different_bandwidths(self, mock_config, sample_state):
        """Test process_sweep with different bandwidth values."""
        sim = PhaserServerSim()

        # Test with zero bandwidth
        state = sample_state.copy()
        state["BW"] = 0
        result1 = sim.process_sweep(state)

        # Test with nonzero bandwidth
        state["BW"] = 20
        result2 = sim.process_sweep(state)

        # Both should produce results (angles may differ)
        assert len(result1["ArrayGain"]) > 0
        assert len(result2["ArrayGain"]) > 0

    def test_process_sweep_with_different_steering_resolutions(self, mock_config, sample_state):
        """Test process_sweep with different steering resolutions."""
        sim = PhaserServerSim()

        # Test with fine resolution
        state = sample_state.copy()
        state["steer_res"] = 0.5
        result1 = sim.process_sweep(state)

        # Test with coarse resolution
        state["steer_res"] = 5.0
        result2 = sim.process_sweep(state)

        assert len(result1["ArrayGain"]) > 0
        assert len(result2["ArrayGain"]) > 0


@pytest.mark.unit
class TestDefaultSerializer:
    """Test JSON serialization helper."""

    def test_numpy_array_serialization(self):
        """Test serialization of numpy arrays."""
        arr = np.array([1.0, 2.0, 3.0])
        result = default_serializer(arr)
        assert isinstance(result, list)
        assert result == [1.0, 2.0, 3.0]

    def test_numpy_generic_types(self):
        """Test serialization of numpy generic types."""
        assert isinstance(default_serializer(np.float64(1.5)), float)
        assert isinstance(default_serializer(np.int32(5)), int)
        assert isinstance(default_serializer(np.bool_(True)), bool)

    def test_complex_number_serialization(self):
        """Test complex numbers are converted to magnitude."""
        result = default_serializer(1 + 2j)
        assert isinstance(result, float)
        assert np.isclose(result, np.sqrt(5.0))

