# Test Suite Summary & Status

## Overall Stats
- **Total Tests**: 67
- **Passing**: 36 (54%)
- **Failing**: 31 (46%)
- **Duration**: ~100 seconds

## Test Results by Category

###  Unit Tests - Passing
✅ **Calibration Loading** (10/10 passing)
- All phase_cal, gain_cal, channel_cal, hb100_cal tests pass
- Fallback behavior works correctly

✅ **Service Simulation** (11/14 passing)
- PhaserServerSim initialization and basic sweep processing work
- JSON response structure validation passes most tests

✅ **Backend Service**  (8/8 passing)
- Service initialization and cleanup works
- Startup/shutdown lifecycle is correct

### Issues Identified

#### 1. **API/WebSocket Tests** - 18 failures
- **Root Cause**: `service.startup()` is not using simulation mode
- **Impact**: Tests try to connect to real hardware and fail with "No device found"
- **Fix Needed**: Update test_api.py fixture to use `sim_mode=True`

#### 2. **Configuration Tests** - 6 failures
- **Root Cause**: Mock config fixture not being properly applied to resolve_hardware_uris()
- **Impact**: Config assertions fail because mocked values aren't used
- **Fix Needed**: mock_config fixture needs to patch config module before resolve_hardware_uris() is imported

#### 3. **NumPy 2.0 Compatibility** - 3 failures
- **Root Cause**: `np.float_` removed in NumPy 2.0, using old type in default_serializer
- **Impact**: Serialization tests fail with AttributeError
- **Fix Needed**: Update phaser_service.py line ~522 to use `np.floating` instead

#### 4. **FFT Tests** - 3 failures  
- **Root Cause**: spec_est() frequency axis returns shifted frequencies from fftshift
- **Impact**: Tests assume DC at index 0, but get shifted frequencies
- **Fix Needed**: Update test expectations or use unshifted FFT

#### 5. **Static Phase Mode** - 1 failure
- **Root Cause**: PhaserServerSim doesn't add gain when mode is "Static Phase" with empty PhaseValues
- **Impact**: Test expects 1 gain value, gets 0
- **Fix Needed**: Adjust test expectation or check PhaserServerSim logic

##Next Steps
Priority fixes (in order):

1. **Fix NumPy compatibility** - Edit phaser_service.py default_serializer
2. **Fix API tests** - Update test_api.py to use sim_mode
3. **Fix config tests** - Mock config module at import time
4. **Fix spec_est tests** - Adjust test expectations for FFT frequency handling  
5. **Fix static phase test** - Verify expected behavior

