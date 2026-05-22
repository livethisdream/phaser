"""
Pytest configuration and shared fixtures for Phaser test suite.
"""
import json
import os
import pickle
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


# Add the repo root to sys.path so we can import phaser modules
REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def temp_config_dir():
    """Create a temporary directory for test configuration files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def mock_config(temp_config_dir, monkeypatch):
    """Mock the config module with test values."""
    # Save original sys.modules state
    original_config = sys.modules.get("config")

    # Create a mock config module
    mock_cfg = MagicMock()
    mock_cfg.uri_mode = "prefer_config"
    mock_cfg.rpi_uri = "ip:test.local"
    mock_cfg.sdr_uri = "ip:test.local:50901"
    mock_cfg.SignalFreq = 10.525e9
    mock_cfg.Tx_freq = 2.2e9
    mock_cfg.Rx_freq = 2.2e9
    mock_cfg.SampleRate = 3e6
    mock_cfg.Rx_gain = 30
    mock_cfg.Tx_gain = -10
    mock_cfg.Averages = 1
    mock_cfg.d = 0.014
    mock_cfg.buffer_size = 1024 * 16
    mock_cfg.Rx1_cal = 0.0
    mock_cfg.Rx2_cal = 0.0
    mock_cfg.Rx3_cal = 0.0
    mock_cfg.Rx4_cal = 0.0
    mock_cfg.Rx5_cal = 0.0
    mock_cfg.Rx6_cal = 0.0
    mock_cfg.Rx7_cal = 0.0
    mock_cfg.Rx8_cal = 0.0

    sys.modules["config"] = mock_cfg

    # Patch os.path for config module discovery
    monkeypatch.setenv("PHASER_RPI_URI", "")
    monkeypatch.setenv("PHASER_SDR_URI", "")

    yield mock_cfg

    # Restore original state
    if original_config:
        sys.modules["config"] = original_config


@pytest.fixture
def calibration_dir(temp_config_dir, monkeypatch):
    """Create a temporary directory with mock calibration files."""
    cal_dir = temp_config_dir

    # Create mock phase calibration file
    phase_cal = [0.0, 1.5, -1.2, 0.8, -0.5, 1.1, -0.3, 0.6]
    with open(cal_dir / "phase_cal_val.pkl", "wb") as f:
        pickle.dump(phase_cal, f)

    # Create mock gain calibration file
    gain_cal = [1.0, 1.05, 0.95, 1.02, 0.98, 1.01, 0.99, 1.03]
    with open(cal_dir / "gain_cal_val.pkl", "wb") as f:
        pickle.dump(gain_cal, f)

    # Create mock channel calibration file
    channel_cal = [0.5, -0.5]
    with open(cal_dir / "channel_cal_val.pkl", "wb") as f:
        pickle.dump(channel_cal, f)

    # Create mock hb100_cal.txt file
    with open(cal_dir / "hb100_cal.txt", "w") as f:
        f.write("10.525e9")

    # Mock the repo path function to point to our temp directory
    def mock_repo_path(filename):
        return str(cal_dir / filename)

    monkeypatch.setattr("phaser_functions._repo_path", mock_repo_path)

    yield cal_dir


@pytest.fixture
def mock_adi_module(mocker):
    """Mock the adi module for hardware-independent testing."""
    mock_adi = MagicMock()

    # Mock GPIO
    mock_gpio = MagicMock()
    mock_gpio.gpio_vctrl_1 = 1
    mock_gpio.gpio_vctrl_2 = 1
    mock_gpio.gpio_div_mr = 1
    mock_gpio.gpio_div_s0 = 0
    mock_gpio.gpio_div_s1 = 0
    mock_gpio.gpio_div_s2 = 0
    mock_gpio.gpio_tx_sw = 0
    mock_adi.one_bit_adc_dac.return_value = mock_gpio

    # Mock SDR (Pluto)
    mock_sdr = MagicMock()
    mock_sdr.sample_rate = 3e6
    mock_sdr.tx_lo = 2.2e9
    mock_sdr.rx_lo = 2.2e9
    mock_sdr.tx_gain0 = -10
    mock_sdr.rx_gain0 = 30
    mock_sdr.rx_gain1 = 30
    mock_sdr.filter_source = "Auto"
    mock_sdr.rx_buffer_size = 1024 * 16
    mock_sdr.rx0_enabled = True
    mock_sdr.rx1_enabled = True
    mock_adi.Pluto.return_value = mock_sdr

    # Mock LO (ADF4159)
    mock_lo = MagicMock()
    mock_lo.frequency = 10.525e9 + 2.2e9
    mock_adi.adf4159.return_value = mock_lo

    # Mock ADAR Array
    mock_device = MagicMock()
    mock_array = MagicMock()
    mock_array.devices = {"BEAM0": mock_device, "BEAM1": mock_device}
    mock_adi.adar1000_array.return_value = mock_array

    mocker.patch.dict("sys.modules", {"adi": mock_adi})

    return mock_adi


@pytest.fixture
def sample_state():
    """Provide a sample state dictionary for sweep operations."""
    return {
        "cmd": "sweep",
        "SignalFreq": 10.525e9,
        "Rx_freq": 2.2e9,
        "Rx_gain": 30,
        "Tx_gain": -10,
        "Averages": 1,
        "d": 0.014,
        "mode": "Beam Sweep",
        "BW": 10,
        "steer_res": 1.0,
        "bits": 7,
        "phaseList": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        "PhaseValues": [-45.0, -30.0, -15.0, 0.0, 15.0, 30.0, 45.0],
        "B0_Gain": 1.0,
        "B1_Gain": 1.0,
        "Beam0_Phase": 0.0,
        "Beam1_Phase": 0.0,
        "gainList": [100, 100, 100, 100, 100, 100, 100, 100],
        "Tx_mode": "Transmit Disabled",
    }


@pytest.fixture
def response_serializer():
    """Provide the default JSON serializer for response validation."""
    from phaser_service import default_serializer
    return default_serializer


# Test markers
def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers", "unit: mark test as a unit test"
    )
    config.addinivalue_line(
        "markers", "integration: mark test as an integration test"
    )
    config.addinivalue_line(
        "markers", "slow: mark test as slow"
    )

