"""
Unit tests for configuration loading and hardware URI resolution.
"""
from unittest.mock import MagicMock, patch

import pytest

# Import after sys.path is set up
import phaser_service


@pytest.mark.unit
class TestURIResolution:
    """Test hardware URI resolution with different configuration modes."""

    def test_auto_mode_phaser_hostname(self, monkeypatch):
        """Test auto mode with 'phaser' in hostname uses localhost."""
        # Create a fresh mock config module
        mock_config = MagicMock()
        mock_config.uri_mode = "auto"
        monkeypatch.setattr("phaser_service.config", mock_config)
        monkeypatch.setattr("phaser_service._detect_hostname", lambda: "phaser.local")

        hostname, rpi_uri, sdr_uri = phaser_service.resolve_hardware_uris()

        assert "ip:localhost" == rpi_uri
        assert "ip:192.168.2.1" == sdr_uri

    def test_auto_mode_laptop_hostname(self, monkeypatch):
        """Test auto mode with non-phaser hostname uses phaser.local."""
        mock_config = MagicMock()
        mock_config.uri_mode = "auto"
        monkeypatch.setattr("phaser_service.config", mock_config)
        monkeypatch.setattr("phaser_service._detect_hostname", lambda: "my-laptop")

        hostname, rpi_uri, sdr_uri = phaser_service.resolve_hardware_uris()

        assert "ip:phaser.local" == rpi_uri
        assert "ip:phaser.local:50901" == sdr_uri

    def test_prefer_config_mode_with_config(self, monkeypatch):
        """Test prefer_config mode uses configured URIs when present."""
        mock_config = MagicMock()
        mock_config.uri_mode = "prefer_config"
        mock_config.rpi_uri = "ip:custom.host"
        mock_config.sdr_uri = "ip:custom.host:50901"
        monkeypatch.setattr("phaser_service.config", mock_config)

        hostname, rpi_uri, sdr_uri = phaser_service.resolve_hardware_uris()

        assert "ip:custom.host" == rpi_uri
        assert "ip:custom.host:50901" == sdr_uri

    def test_prefer_config_mode_fallback_to_auto(self, monkeypatch):
        """Test prefer_config mode falls back to auto when config is None."""
        mock_config = MagicMock()
        mock_config.uri_mode = "prefer_config"
        mock_config.rpi_uri = None
        mock_config.sdr_uri = None
        monkeypatch.setattr("phaser_service.config", mock_config)
        monkeypatch.setattr("phaser_service._detect_hostname", lambda: "my-laptop")

        hostname, rpi_uri, sdr_uri = phaser_service.resolve_hardware_uris()

        assert "ip:phaser.local" == rpi_uri
        assert "ip:phaser.local:50901" == sdr_uri

    def test_custom_mode_with_both_uris(self, monkeypatch):
        """Test custom mode requires both URIs to be specified."""
        mock_config = MagicMock()
        mock_config.uri_mode = "custom"
        mock_config.rpi_uri = "ip:explicit.host"
        mock_config.sdr_uri = "ip:explicit.host:50901"
        monkeypatch.setattr("phaser_service.config", mock_config)

        hostname, rpi_uri, sdr_uri = phaser_service.resolve_hardware_uris()

        assert "ip:explicit.host" == rpi_uri
        assert "ip:explicit.host:50901" == sdr_uri

    def test_custom_mode_missing_rpi_uri_raises(self, monkeypatch):
        """Test custom mode raises ValueError if rpi_uri is missing."""
        mock_config = MagicMock()
        mock_config.uri_mode = "custom"
        mock_config.rpi_uri = None
        mock_config.sdr_uri = "ip:host:50901"
        monkeypatch.setattr("phaser_service.config", mock_config)

        with pytest.raises(ValueError, match="uri_mode='custom'"):
            phaser_service.resolve_hardware_uris()

    def test_custom_mode_missing_sdr_uri_raises(self, monkeypatch):
        """Test custom mode raises ValueError if sdr_uri is missing."""
        mock_config = MagicMock()
        mock_config.uri_mode = "custom"
        mock_config.rpi_uri = "ip:host"
        mock_config.sdr_uri = None
        monkeypatch.setattr("phaser_service.config", mock_config)

        with pytest.raises(ValueError, match="uri_mode='custom'"):
            phaser_service.resolve_hardware_uris()

    def test_env_override_highest_precedence(self, monkeypatch):
        """Test environment variables override all configuration."""
        mock_config = MagicMock()
        mock_config.uri_mode = "custom"
        mock_config.rpi_uri = "ip:config.host"
        mock_config.sdr_uri = "ip:config.host:50901"
        monkeypatch.setattr("phaser_service.config", mock_config)

        monkeypatch.setenv("PHASER_RPI_URI", "ip:env.host")
        monkeypatch.setenv("PHASER_SDR_URI", "ip:env.host:50901")

        hostname, rpi_uri, sdr_uri = phaser_service.resolve_hardware_uris()

        assert "ip:env.host" == rpi_uri
        assert "ip:env.host:50901" == sdr_uri

    def test_env_rpi_only_ignored_without_sdr(self, monkeypatch):
        """Test environment URIs must both be present to override."""
        mock_config = MagicMock()
        mock_config.uri_mode = "prefer_config"
        mock_config.rpi_uri = "ip:config.host"
        mock_config.sdr_uri = "ip:config.host:50901"
        monkeypatch.setattr("phaser_service.config", mock_config)

        monkeypatch.setenv("PHASER_RPI_URI", "ip:env.host")
        monkeypatch.setenv("PHASER_SDR_URI", "")

        hostname, rpi_uri, sdr_uri = phaser_service.resolve_hardware_uris()

        # Should use config since env not both set
        assert "ip:config.host" == rpi_uri
        assert "ip:config.host:50901" == sdr_uri

