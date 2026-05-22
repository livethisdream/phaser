"""
Unit tests for phaser_functions.py (calibration loading, signal processing).
"""
import pickle
import json
from pathlib import Path

import numpy as np
import pytest

from phaser_functions import (
    load_channel_cal,
    load_gain_cal,
    load_hb100_cal,
    load_phase_cal,
    save_channel_cal,
    save_gain_cal,
    save_hb100_cal,
    save_phase_cal,
    spec_est,
)


@pytest.mark.unit
class TestCalibrationLoading:
    """Test calibration file loading with fallback behavior."""

    def test_load_phase_cal_with_file(self, calibration_dir):
        """Test loading valid phase calibration file."""
        phase_cal = load_phase_cal()

        assert len(phase_cal) == 8
        assert isinstance(phase_cal, list)
        # Check that values match what we saved in fixture
        assert phase_cal[0] == 0.0
        assert phase_cal[1] == 1.5

    def test_load_phase_cal_without_file(self, temp_config_dir, monkeypatch):
        """Test phase calibration falls back to zeros when file missing."""
        def mock_repo_path(filename):
            return str(temp_config_dir / "nonexistent.pkl")

        monkeypatch.setattr("phaser_functions._repo_path", mock_repo_path)

        phase_cal = load_phase_cal()

        assert len(phase_cal) == 8
        assert all(v == 0.0 for v in phase_cal)

    def test_load_phase_cal_with_custom_default(self, temp_config_dir, monkeypatch):
        """Test phase calibration custom default values."""
        def mock_repo_path(filename):
            return str(temp_config_dir / "nonexistent.pkl")

        monkeypatch.setattr("phaser_functions._repo_path", mock_repo_path)

        custom_default = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
        phase_cal = load_phase_cal(default=custom_default)

        assert phase_cal == custom_default

    def test_load_gain_cal_with_file(self, calibration_dir):
        """Test loading valid gain calibration file."""
        gain_cal = load_gain_cal()

        assert len(gain_cal) == 8
        assert isinstance(gain_cal, list)
        assert gain_cal[0] == 1.0
        assert gain_cal[1] == 1.05

    def test_load_gain_cal_without_file(self, temp_config_dir, monkeypatch):
        """Test gain calibration falls back to ones when file missing."""
        def mock_repo_path(filename):
            return str(temp_config_dir / "nonexistent.pkl")

        monkeypatch.setattr("phaser_functions._repo_path", mock_repo_path)

        gain_cal = load_gain_cal()

        assert len(gain_cal) == 8
        assert all(v == 1.0 for v in gain_cal)

    def test_load_channel_cal_with_file(self, calibration_dir):
        """Test loading valid channel calibration file."""
        channel_cal = load_channel_cal()

        assert len(channel_cal) == 2
        assert isinstance(channel_cal, list)
        assert channel_cal[0] == 0.5
        assert channel_cal[1] == -0.5

    def test_load_channel_cal_without_file(self, temp_config_dir, monkeypatch):
        """Test channel calibration falls back to zeros when file missing."""
        def mock_repo_path(filename):
            return str(temp_config_dir / "nonexistent.pkl")

        monkeypatch.setattr("phaser_functions._repo_path", mock_repo_path)

        channel_cal = load_channel_cal()

        assert len(channel_cal) == 2
        assert all(v == 0.0 for v in channel_cal)

    def test_load_hb100_cal_with_file(self, calibration_dir):
        """Test loading HB100 calibration frequency."""
        freq = load_hb100_cal()

        assert freq == 10.525e9

    def test_load_hb100_cal_without_file(self, temp_config_dir, monkeypatch):
        """Test HB100 calibration raises FileNotFoundError when missing."""
        def mock_repo_path(filename):
            return str(temp_config_dir / "nonexistent.txt")

        monkeypatch.setattr("phaser_functions._repo_path", mock_repo_path)

        with pytest.raises(FileNotFoundError):
            load_hb100_cal()

    def test_load_hb100_cal_invalid_content(self, calibration_dir, monkeypatch):
        """Test HB100 calibration raises FileNotFoundError on invalid content."""
        # Create file with invalid content
        cal_file = calibration_dir / "hb100_cal.txt"
        with open(cal_file, "w") as f:
            f.write("not-a-number")

        def mock_repo_path(filename):
            return str(cal_file)

        monkeypatch.setattr("phaser_functions._repo_path", mock_repo_path)

        with pytest.raises(FileNotFoundError):
            load_hb100_cal()

    def test_save_and_load_json_calibrations(self, temp_config_dir, monkeypatch):
        """JSON calibration storage round-trips phase/gain/channel values."""
        def mock_repo_path(filename):
            return str(temp_config_dir / filename)

        monkeypatch.setattr("phaser_functions._repo_path", mock_repo_path)

        phase = [float(i) for i in range(8)]
        gain = [1.0 + (i * 0.01) for i in range(8)]
        channel = [0.25, -0.25]

        save_phase_cal(phase)
        save_gain_cal(gain)
        save_channel_cal(channel)

        assert load_phase_cal() == phase
        assert load_gain_cal() == gain
        assert load_channel_cal() == channel

        cal_json = temp_config_dir / "calibration.json"
        payload = json.loads(cal_json.read_text(encoding="utf-8"))
        assert payload["phase_cal"] == phase
        assert payload["gain_cal"] == gain
        assert payload["channel_cal"] == channel

    def test_save_hb100_cal_writes_json_only(self, temp_config_dir, monkeypatch):
        """HB100 save writes JSON and does not create new legacy txt file."""
        def mock_repo_path(filename):
            return str(temp_config_dir / filename)

        monkeypatch.setattr("phaser_functions._repo_path", mock_repo_path)

        save_hb100_cal(10.123e9)

        assert load_hb100_cal() == 10.123e9
        assert not (temp_config_dir / "hb100_cal.txt").exists()
        payload = json.loads((temp_config_dir / "calibration.json").read_text(encoding="utf-8"))
        assert payload["hb100_freq_hz"] == 10.123e9


@pytest.mark.unit
class TestSpecEstimation:
    """Test spectrum estimation (FFT, magnitude, amplitude conversion)."""

    def test_spec_est_basic_operation(self):
        """Test basic spectrum estimation operation."""
        # Create simple test signal (DC component)
        data = np.ones(128) * 100
        sample_rate = 1e6

        amps, freqs = spec_est(data, sample_rate, ref=2**12)

        assert len(freqs) == len(data)
        assert len(amps) == len(data)
        peak_idx = int(np.argmax(amps))
        assert abs(freqs[peak_idx]) < 1e-9

    def test_spec_est_empty_data(self):
        """Test spectrum estimation with empty data."""
        data = np.array([])
        sample_rate = 1e6

        amps, freqs = spec_est(data, sample_rate)

        assert len(freqs) == 0
        assert len(amps) == 0

    def test_spec_est_sine_wave(self):
        """Test spectrum estimation with sine wave."""
        # Create 1 kHz sine wave at 1 MSps
        t = np.arange(1000) / 1e6
        f_signal = 1000
        data = np.sin(2 * np.pi * f_signal * t) * (2**11)

        amps, freqs = spec_est(data, 1e6)

        assert len(freqs) == len(data)
        assert len(amps) == len(data)
        # Signal should have energy around 1 kHz
        peak_idx = np.argmax(amps)
        peak_freq = abs(freqs[peak_idx])
        # Peak may not be exactly at f_signal due to windowing, but should be close
        assert abs(peak_freq - f_signal) < 1200

    def test_spec_est_single_sample(self):
        """Test spectrum estimation with single sample."""
        data = np.array([100.0])
        sample_rate = 1e6

        amps, freqs = spec_est(data, sample_rate)

        assert len(freqs) == 1
        assert len(amps) == 1

    def test_spec_est_frequency_axis(self):
        """Test that frequency axis is correctly computed."""
        num_samples = 256
        sample_rate = 1e6

        data = np.ones(num_samples)
        amps, freqs = spec_est(data, sample_rate)

        # Check frequency bin spacing
        freq_step = sample_rate / num_samples
        sorted_freqs = np.sort(freqs)
        assert np.isclose(np.mean(np.diff(sorted_freqs)), freq_step, rtol=1e-3)

    def test_spec_est_float32_input(self):
        """Test spectrum estimation with float32 data."""
        data = np.array([1.0, 2.0, 3.0, 4.0], dtype=np.float32)
        sample_rate = 1e6

        amps, freqs = spec_est(data, sample_rate)

        assert len(freqs) == 4
        assert len(amps) == 4

    def test_spec_est_complex_input(self):
        """Test spectrum estimation with complex data."""
        data = np.array([1+1j, 2+2j, 3+3j, 4+4j])
        sample_rate = 1e6

        amps, freqs = spec_est(data, sample_rate)

        assert len(freqs) == 4
        assert len(amps) == 4

