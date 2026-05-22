"""
Integration tests for FastAPI endpoints and WebSocket communication.
"""
import json

import pytest
from fastapi.testclient import TestClient

from phaser_service import BackendService, default_serializer


@pytest.fixture
def backend_service(mock_config):
    """Provide a BackendService instance in simulation mode."""
    service = BackendService(sim_mode=True)
    service.startup()
    yield service
    service.shutdown()


@pytest.fixture
def test_app(backend_service):
    """Create a test FastAPI app with initialized service."""
    import phaser_server
    phaser_server.service = backend_service
    app = phaser_server.app
    return app


@pytest.fixture
def test_client(test_app, backend_service):
    """Provide a FastAPI test client with service initialized."""
    return TestClient(test_app)


@pytest.mark.integration
class TestAPIEndpoints:
    """Test FastAPI HTTP endpoints."""

    def test_get_api_state(self, mock_config, test_client, backend_service):
        """Test GET /api/state endpoint."""
        response = test_client.get("/api/state")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "data" in data
        assert "SignalFreq" in data["data"]
        assert "Rx_freq" in data["data"]
        assert "Rx_gain" in data["data"]
        assert "Tx_gain" in data["data"]

    def test_get_api_state_structure(self, mock_config, test_client, backend_service):
        """Test GET /api/state returns expected data structure."""
        response = test_client.get("/api/state")
        data = response.json()

        expected_keys = [
            "SignalFreq", "Rx_freq", "Rx_gain", "Tx_gain",
            "Averages", "d", "BW", "sim_mode", "lab_presets_supported"
        ]
        for key in expected_keys:
            assert key in data["data"], f"Missing key: {key}"

    def test_get_lab_preset_valid_index(self, mock_config, test_client, backend_service):
        """Test GET /api/lab/{lab_idx} with valid index."""
        response = test_client.get("/api/lab/1")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"

    def test_get_lab_preset_invalid_index(self, mock_config, test_client, backend_service):
        """Test GET /api/lab/{lab_idx} with invalid index."""
        # Try to get preset at invalid index
        response = test_client.get("/api/lab/999")

        assert response.status_code == 200
        assert response.json()["status"] == "error"

    def test_get_calibration_status(self, mock_config, test_client, backend_service):
        """Test GET /api/calibration/status endpoint."""
        response = test_client.get("/api/calibration/status")

        assert response.status_code == 200
        data = response.json()
        assert "running" in data or "status" in data

    def test_post_calibration_task(self, mock_config, test_client, backend_service):
        """Test POST /api/calibration/{task_name} endpoint."""
        response = test_client.post("/api/calibration/test_task")

        assert response.status_code == 200
        data = response.json()
        assert "status" in data or "running" in data



@pytest.mark.integration
class TestWebSocketEndpoint:
    """Test WebSocket /ws endpoint."""

    def test_websocket_connection(self, mock_config, test_client, backend_service):
        """Test WebSocket connection is accepted."""
        with test_client.websocket_connect("/ws") as websocket:
            # Should not raise
            assert websocket is not None

    def test_websocket_sweep_command_success(self, mock_config, test_client, backend_service, sample_state):
        """Test WebSocket processes sweep command successfully."""
        with test_client.websocket_connect("/ws") as websocket:
            # Send sweep command
            message = {"cmd": "sweep", "state": sample_state}
            websocket.send_text(json.dumps(message))

            # Receive response
            response = websocket.receive_text()
            data = json.loads(response)

            assert data["status"] == "ok"
            assert "data" in data
            assert "ArrayGain" in data["data"]
            assert "ArrayDelta" in data["data"]

    def test_websocket_sweep_response_structure(self, mock_config, test_client, backend_service, sample_state):
        """Test WebSocket sweep response has expected structure."""
        with test_client.websocket_connect("/ws") as websocket:
            message = {"cmd": "sweep", "state": sample_state}
            websocket.send_text(json.dumps(message))
            response = websocket.receive_text()
            data = json.loads(response)

            expected_data_keys = [
                "ArrayGain", "ArrayDelta", "ArrayBeamPhase",
                "ArrayAngle", "ArrayError", "max_gain", "xf"
            ]
            for key in expected_data_keys:
                assert key in data["data"], f"Missing key: {key}"

    def test_websocket_invalid_json(self, mock_config, test_client, backend_service):
        """Test WebSocket handles invalid JSON gracefully."""
        with test_client.websocket_connect("/ws") as websocket:
            # Send invalid JSON
            websocket.send_text("not valid json")

            # Should receive error response
            response = websocket.receive_text()
            data = json.loads(response)

            assert data["status"] == "error"
            assert "message" in data

    def test_websocket_unknown_command(self, mock_config, test_client, backend_service):
        """Test WebSocket handles unknown command gracefully."""
        with test_client.websocket_connect("/ws") as websocket:
            message = {"cmd": "unknown_command"}
            websocket.send_text(json.dumps(message))

            response = websocket.receive_text()
            data = json.loads(response)

            assert data["status"] == "error"
            assert "Unknown command" in data["message"]

    def test_websocket_missing_cmd_field(self, mock_config, test_client, backend_service):
        """Test WebSocket handles missing cmd field."""
        with test_client.websocket_connect("/ws") as websocket:
            message = {"state": {}}  # No 'cmd' field
            websocket.send_text(json.dumps(message))

            response = websocket.receive_text()
            data = json.loads(response)

            assert data["status"] == "error"

    def test_websocket_multiple_messages(self, mock_config, test_client, backend_service, sample_state):
        """Test WebSocket handles multiple consecutive messages."""
        with test_client.websocket_connect("/ws") as websocket:
            # Send first sweep
            message1 = {"cmd": "sweep", "state": sample_state}
            websocket.send_text(json.dumps(message1))
            response1 = websocket.receive_text()
            assert json.loads(response1)["status"] == "ok"

            # Send second sweep
            message2 = {"cmd": "sweep", "state": sample_state}
            websocket.send_text(json.dumps(message2))
            response2 = websocket.receive_text()
            assert json.loads(response2)["status"] == "ok"

    def test_websocket_sweep_with_modified_state(self, mock_config, test_client, backend_service, sample_state):
        """Test WebSocket sweep with different state values."""
        with test_client.websocket_connect("/ws") as websocket:
            # Modify state
            state = sample_state.copy()
            state["Rx_gain"] = 20
            state["SignalFreq"] = 11e9

            message = {"cmd": "sweep", "state": state}
            websocket.send_text(json.dumps(message))

            response = websocket.receive_text()
            data = json.loads(response)

            assert data["status"] == "ok"
            assert "data" in data



@pytest.mark.integration
class TestJSONSerialization:
    """Test JSON serialization of responses."""

    def test_sweep_response_serializable(self, mock_config, test_client, backend_service, sample_state):
        """Test that sweep response is valid JSON with all numpy types converted."""
        with test_client.websocket_connect("/ws") as websocket:
            message = {"cmd": "sweep", "state": sample_state}
            websocket.send_text(json.dumps(message))

            response_text = websocket.receive_text()
            # Should not raise JSON parse error
            data = json.loads(response_text)

            # Re-serialize to verify all types are JSON-compatible
            json_str = json.dumps(data, default=default_serializer)
            assert isinstance(json_str, str)

    def test_api_state_response_serializable(self, mock_config, test_client, backend_service):
        """Test that API state response is valid JSON."""
        response = test_client.get("/api/state")
        assert response.status_code == 200

        # Should be automatically JSON serializable by fastapi
        data = response.json()
        json_str = json.dumps(data, default=default_serializer)
        assert isinstance(json_str, str)



@pytest.mark.integration
class TestCORSConfiguration:
    """Test CORS middleware is properly configured."""

    def test_cors_headers_present(self, mock_config, test_client, backend_service):
        """Test CORS headers are present in responses."""
        response = test_client.get("/api/state", headers={"Origin": "http://localhost:5173"})

        # Should have CORS headers
        assert "access-control-allow-origin" in response.headers

    def test_cors_allows_all_origins(self, mock_config, test_client, backend_service):
        """Test CORS allows all origins."""
        origin = "http://localhost:5173"
        response = test_client.get("/api/state", headers={"Origin": origin})

        assert response.headers.get("access-control-allow-origin") == origin


