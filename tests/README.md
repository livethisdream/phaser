# Phaser Test Suite

Comprehensive test coverage for the Phaser backend, including unit tests, integration tests, and API validation.

## Quick Start

### Installation

First, install test dependencies:

```bash
# Using pip
pip install -e ".[test]"

# Or using uv
uv pip install -e ".[test]"
```

### Run All Tests

```bash
pytest
```

### Run With Coverage

```bash
pytest --cov=. --cov-report=html
```

This generates an HTML coverage report in `htmlcov/index.html`.

## Test Organization

### Test Files

- **`test_config.py`** — Configuration and hardware URI resolution
  - URI resolution with different modes (auto, prefer_config, custom)
  - Environment variable override behavior
  - Hostname detection

- **`test_phaser_functions.py`** — Signal processing and calibration  
  - Calibration file loading (phase, gain, channel, HB100)
  - Fallback behavior when files are missing
  - Spectrum estimation (FFT, magnitude conversion)
  - Edge cases (empty data, single sample, different input types)

- **`test_phaser_service.py`** — Service layer and simulation backend
  - `PhaserServerSim` initialization and state management
  - `BackendService` startup/shutdown lifecycle
  - Sweep command processing in simulation mode
  - Phase sweep computation with different modes
  - JSON serialization of numpy types

- **`test_api.py`** — FastAPI endpoints and WebSocket
  - HTTP GET endpoints (`/api/state`, `/api/lab/{idx}`, `/api/calibration/status`)
  - HTTP POST endpoints (`/api/calibration/{task_name}`)
  - WebSocket connection and message handling
  - Sweep command processing over WebSocket
  - Error handling (invalid JSON, unknown commands)
  - CORS configuration

- **`conftest.py`** — Pytest fixtures and configuration
  - Temporary config directories
  - Mock configuration module
  - Mock calibration files
  - Mock hardware (adi module)
  - Sample state dictionaries

## Test Markers

Tests are marked with categories for selective execution:

```bash
# Run only unit tests (fast, no hardware)
pytest -m unit

# Run only integration tests (may start services)
pytest -m integration

# Run slow tests
pytest -m slow

# Run everything EXCEPT slow tests
pytest -m "not slow"
```

## Key Testing Strategies

### 1. Configuration Isolation

Each test gets its own mock configuration module, preventing cross-test pollution:

```python
def test_something(mock_config):
    # mock_config is a fresh MagicMock per test
    assert mock_config.SignalFreq == 10.525e9
```

### 2. Temporary Calibration Files

Calibration file tests use `calibration_dir` fixture which:
- Creates temporary .pkl and .txt files
- Mocks `_repo_path()` to point to temp directory
- Cleans up automatically after test

```python
def test_load_cal(calibration_dir):
    phase_cal = load_phase_cal()
    # Uses mocked files from calibration_dir
```

### 3. Hardware Mocking

The `mock_adi_module` fixture patches the `adi` module entirely:
- Mocks `one_bit_adc_dac` (GPIO)
- Mocks `Pluto` (SDR)
- Mocks `adf4159` (LO)
- Mocks `adar1000_array` (ADAR)

Tests using `PhaserServerSim` don't require hardware mocking (simulation only).

### 4. WebSocket Testing

Uses FastAPI's `TestClient` for synchronous WebSocket testing:

```python
with test_client.websocket_connect("/ws") as websocket:
    websocket.send_text(json.dumps(message))
    response = websocket.receive_text()
    data = json.loads(response)
```

## Coverage Goals

Current test coverage includes:

- **phaser_functions.py**: ~95% (calibration loading, FFT)
- **phaser_service.py**: ~85% (BackendService, PhaserServerSim, sweep processing)
- **phaser_server.py**: ~80% (API endpoints, WebSocket routing)
- **Configuration**: ~90% (URI resolution, config modes)

To check coverage:

```bash
pytest --cov=. --cov-report=term-missing
```

## Running Specific Tests

```bash
# Single test class
pytest tests/test_config.py::TestURIResolution

# Single test method
pytest tests/test_config.py::TestURIResolution::test_auto_mode_phaser_hostname

# Tests matching a name pattern
pytest -k "uri_resolution"

# Tests matching multiple patterns
pytest -k "load_cal and not hb100"
```

## Debugging Tests

### Verbose Output

```bash
pytest -vv tests/test_config.py
```

### Show Print Statements

```bash
pytest -s tests/test_config.py::TestURIResolution::test_auto_mode_phaser_hostname
```

### Drop Into Debugger on Failure

```bash
pytest --pdb tests/test_config.py
```

### Show Local Variables on Failure

```bash
pytest -l tests/test_config.py
```

## Known Limitations

1. **Hardware Tests Not Included** — Tests using real hardware (GPIO, SDR, ADAR) are excluded to enable CI/CD without hardware. These are tested manually in development.

2. **Async Calibration Tasks** — Calibration subprocess tasks (`phaser_cal.py`) are not tested as they require subprocess mocking and are environment-specific.

3. **Frontend Integration** — Frontend WebSocket behavior (Vite, browser APIs) is not tested; only the backend `/ws` endpoint is tested.

## Contributing New Tests

When adding new tests:

1. **Place in appropriate file** — Add to existing test file or create new `test_<module>.py`

2. **Use markers** — Mark with `@pytest.mark.unit` or `@pytest.mark.integration`

3. **Follow naming** — Test functions must start with `test_`, classes with `Test`

4. **Use fixtures** — Leverage existing fixtures from `conftest.py`:
   ```python
   def test_something(mock_config, sample_state, calibration_dir):
       # Uses fixtures
   ```

5. **Document purpose** — Include docstring explaining what's being tested

6. **Test edge cases** — Include tests for errors, empty inputs, missing files, etc.

Example:

```python
@pytest.mark.unit
class TestNewFeature:
    """Test description."""
    
    def test_normal_case(self, mock_config):
        """Test normal operation."""
        assert True
    
    def test_edge_case(self, mock_config):
        """Test edge case behavior."""
        with pytest.raises(ValueError):
            something_that_raises()
```

## CI/CD Integration

For GitHub Actions or other CI systems:

```yaml
- name: Run Phaser Tests
  run: |
    pip install -e ".[test]"
    pytest --cov=. --cov-report=xml
    
- name: Upload Coverage
  uses: codecov/codecov-action@v3
```

## Performance

- **Unit tests**: < 1 second
- **Integration tests**: < 5 seconds
- **Full suite**: < 10 seconds

Run faster with:

```bash
pytest -n auto  # Parallel execution (requires pytest-xdist)
```

## Troubleshooting

### ImportError: No module named 'phaser_functions'

Ensure you're running pytest from the repo root:

```bash
cd /path/to/Phaser
pytest
```

### ModuleNotFoundError: No module named 'adi'

This is expected in test environments. Tests mock the `adi` module. If you need real hardware testing,install pyadi-iio:

```bash
pip install pyadi-iio
```

### WebSocket TestClient Issues

Ensure FastAPI test client dependency (`httpx`) is installed:

```bash
pip install httpx
```

## References

- [pytest documentation](https://docs.pytest.org/)
- [pytest-asyncio](https://github.com/pytest-dev/pytest-asyncio)
- [FastAPI testing](https://fastapi.tiangolo.com/advanced/testing-websockets/)
- [unittest.mock](https://docs.python.org/3/library/unittest.mock.html)

