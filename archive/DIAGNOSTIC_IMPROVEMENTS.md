# Hardware Connection Diagnostic Fix Summary

## Problem
User reported `phaser_find_hb100.py` failing with "No device found" when running calibration from the GUI. The root cause was unclear because the error message wasn't providing enough diagnostic information.

## Root Cause Analysis
After investigation, we discovered:
1. The script had minimal error reporting - just a generic "No device found" message
2. Network hostname resolution (`phaser.local`) was the actual issue
3. The RPI hostname would not resolve on the user's network, even though the PlutoSDR was reachable
4. Once network diagnostics were attempted, a secondary issue emerged: unavailable SDR debug attributes

## Solution Implemented

### 1. Enhanced Network Diagnostics (phaser_find_hb100.py & phaser_cal.py)

Added pre-flight hostname resolution checks before attempting IIO context creation:

```python
def _test_hostname_resolution(hostname_or_ip):
    """Test if hostname resolves to an IP or if IP is valid.
    
    Returns: (success, result/error, target_hostname_attempted)
    """
    target = hostname_or_ip
    try:
        # Extract hostname from URI format (e.g., "ip:phaser.local" -> "phaser.local")
        if ":" in hostname_or_ip:
            parts = hostname_or_ip.split(":")
            if len(parts) == 3:  # "ip:hostname:port"
                target = parts[1]
            else:  # "ip:hostname"
                target = parts[1]
        
        resolved_ip = socket.gethostbyname(target)
        return True, resolved_ip, target
    except socket.gaierror as e:
        return False, str(e), target
    except Exception as e:
        return False, str(e), target
```

**Benefits:**
- Identifies network issues BEFORE attempting hardware connection
- Reports which specific hostname failed to resolve
- Returns the resolved IP (if successful) for user feedback

### 2. Improved Diagnostic Output

The scripts now display:
- ✅ Which hostnames resolve successfully and to what IPs
- ❌ Which hostnames fail and with what error
- 📋 Actionable solutions based on the specific failure

**Example output:**
```
--- Network Diagnostics ---
[OK] SDR 'phaser.local' resolved to: 192.168.86.39
[FAIL] RPI hostname resolution FAILED for 'phaser.local': [Errno 11001] getaddrinfo failed

Hostname resolution failed. This usually means:
   RPI hostname 'phaser.local' is not reachable
    (mDNS is not working or hardware is offline)

Solutions:
  1. Get RPI static IP: Check your router or network admin panel
  2. Set static IP via environment variables:
     set PHASER_RPI_URI=ip:192.168.X.X  (replace X.X with RPI IP)
     set PHASER_SDR_URI=ip:192.168.86.39  (SDR is at 192.168.86.39)
  3. Verify Phaser hardware is powered and on the network
  4. Check if your network requires special configuration for mDNS
```

### 3. Fixed Windows Encoding Issue

Replaced Unicode characters (✓, ✗, •) with ASCII equivalents:
- `✓` → `[OK]`
- `✗` → `[FAIL]` or `[ERROR]`
- `•` → Direct ASCII text

**Why:** Windows PowerShell uses cp1252 encoding by default, which doesn't support these Unicode characters.

### 4. Fixed SDR Debug Attribute Access

Wrapped SDR debug attribute configuration in try-except block:

```python
try:
    my_sdr._ctrl.debug_attrs["adi,frequency-division-duplex-mode-enable"].value = "1"
    my_sdr._ctrl.debug_attrs["adi,ensm-enable-txnrx-control-enable"].value = "0"
    my_sdr._ctrl.debug_attrs["initialize"].value = "1"
except (AttributeError, KeyError, OSError) as e:
    print(f"[WARNING] Some SDR debug attributes not accessible: {e}")
    print("[INFO] Continuing without those attributes...")
```

**Why:** Some SDR configurations don't expose these debug attributes. The script should be resilient and continue with configuration.

### 5. New Helper Tools Created

#### `diagnose_hardware.py`
Standalone diagnostic script to check hostname detection:
```powershell
python diagnose_hardware.py
```

Shows:
- Your machine's hostname
- Whether you're on-board (Phaser device) or remote (laptop)
- Recommended URIs for your setup

#### `NETWORK_SETUP_GUIDE.md`
Comprehensive guide for users to:
- Understand their network setup
- Find RPI static IP address
- Configure environment variables or config_custom.py
- Troubleshoot common issues

### 6. Documentation Updates

Updated `README.md` to reference the new `NETWORK_SETUP_GUIDE.md` from the URI configuration section.

## Files Modified

1. **phaser_find_hb100.py**
   - Added `_test_hostname_resolution()` function
   - Added pre-flight diagnostic checks
   - Wrapped SDR debug attribute setup in error handling
   - Fixed Unicode encoding issues

2. **phaser_cal.py**
   - Added `_test_hostname_resolution()` function
   - Added pre-flight diagnostic checks
   - Fixed Unicode encoding issues

3. **README.md**
   - Added reference to NETWORK_SETUP_GUIDE.md

## Files Created

1. **diagnose_hardware.py** - Quick hostname detection tool
2. **NETWORK_SETUP_GUIDE.md** - Comprehensive network configuration guide

## User Impact

### Before
- User saw: "No device found" at line 70
- User had no way to diagnose network issues
- No clear path to fix the problem

### After
- **Diagnostic output** shows exactly what resolved and what didn't
- **Actionable next steps** based on specific failure
- **Recommended IP addresses** from successful resolution
- **Helper tools** to diagnose hostname detection
- **Comprehensive guide** (NETWORK_SETUP_GUIDE.md) to resolve network issues

## Testing Results

**Test 1: Hostname Resolution**
- ✅ SDR hostname correctly resolves to 192.168.86.39
- ✅ RPI hostname fails with clear error (Errno 11001)
- ✅ Diagnostic output guides user to solutions

**Test 2: Error Handling**
- ✅ Script continues even if SDR debug attributes unavailable
- ✅ Warning message printed but doesn't crash

**Test 3: Windows Compatibility**
- ✅ No Unicode encoding errors
- ✅ Output readable in Windows PowerShell

## Next Steps for User

1. **Find RPI static IP** using NETWORK_SETUP_GUIDE.md (Step 1)
2. **Configure URIs** using environment variables or config_custom.py (Step 2)
3. **Test with diagnose_hardware.py** to verify configuration works
4. **Trigger Find HB100 from GUI** - should now work with proper network setup

## Summary

This fix transforms a cryptic "No device found" error into a diagnostic system that:
- Identifies exactly which component is unreachable
- Provides the actual IP address of reachable components
- Gives the user actionable next steps with examples
- Includes comprehensive documentation for the entire network setup process

The improvements apply to both `phaser_find_hb100.py` and `phaser_cal.py`, making the entire calibration workflow more robust and user-friendly.

