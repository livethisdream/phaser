# Quick Reference: What Changed and Why

## The Issue
`phaser_find_hb100.py` failed with "No device found" - not enough diagnostic information to determine what was wrong.

## The Solution
Added comprehensive network diagnostics to identify exactly which components are reachable and which aren't.

## Key Changes Made

### 1. Network Diagnostics Function
- Tests hostname resolution BEFORE attempting hardware connection
- Returns success/IP AND error message (if failed)
- Works with URI format (ip:hostname:port)

### 2. Improved Error Messages
**Before:**
```
No device found
```

**After:**
```
[OK] SDR 'phaser.local' resolved to: 192.168.86.39
[FAIL] RPI hostname resolution FAILED for 'phaser.local': [Errno 11001] getaddrinfo failed

Solutions:
  1. Get RPI static IP: Check your router or network admin panel
  2. Set static IP via environment variables:
     set PHASER_RPI_URI=ip:192.168.X.X
     set PHASER_SDR_URI=ip:192.168.86.39  (SDR is at 192.168.86.39)
  ... [more solutions]
```

### 3. Files Modified
- `phaser_find_hb100.py` - Added diagnostics, fixed encoding, added error handling
- `phaser_cal.py` - Added diagnostics, fixed encoding
- `README.md` - Added reference to NETWORK_SETUP_GUIDE.md

### 4. Files Created
- `diagnose_hardware.py` - Quick hostname detection tool
- `NETWORK_SETUP_GUIDE.md` - Complete network configuration guide (with router login instructions, etc.)
- `DIAGNOSTIC_IMPROVEMENTS.md` - Technical implementation details
- `NETWORK_FIX_SUMMARY.md` - This summary

## What You Need To Do

1. **Find RPI static IP**
   - Check your router admin panel for connected device named "phaser"
   - Or: Contact your network admin for the IP

2. **Set environment variables** (temporary, for testing):
   ```powershell
   $env:PHASER_RPI_URI = "ip:192.168.X.X"     # Your RPI IP
   $env:PHASER_SDR_URI = "ip:192.168.86.39"   # From diagnostics
   python phaser_find_hb100.py
   ```

   Or **edit config.py** (permanent):
   ```python
   uri_mode = "prefer_config"
   rpi_uri = "ip:192.168.X.X"       # Your RPI IP
   sdr_uri = "ip:192.168.86.39"     # From diagnostics
   ```

3. **Restart the backend** and try again

## New Helper Tools

| Tool | Command | Purpose |
|------|---------|---------|
| Hostname Detector | `python diagnose_hardware.py` | See if you're on-board or remote, get recommended URIs |
| Network Setup Guide | See `NETWORK_SETUP_GUIDE.md` | Step-by-step instructions for finding RPI IP and configuring |

## The Diagnostic Output Explained

Your current output shows:
```
[OK] SDR 'phaser.local' resolved to: 192.168.86.39
[FAIL] RPI hostname resolution FAILED for 'phaser.local'
```

Translation:
- PlutoSDR (Rx/Tx device) is reachable at 192.168.86.39 ✅
- RPI (control board) hostname is NOT resolving ❌
- Solution: Use static IP for RPI instead of hostname

## When It's Fixed

Once you configure static IP, the output will show:
```
[OK] SDR 'phaser.local' resolved to: 192.168.86.39
[OK] RPI 'phaser.local' resolved to: 192.168.1.100

--- Connecting to Hardware ---
[proceeds to connect and run calibration]
```

## Technical Details

See `DIAGNOSTIC_IMPROVEMENTS.md` for:
- Implementation details of the diagnostics function
- How the 3-tuple return value works
- Why we handle missing debug attributes
- Windows encoding fixes explained

## Questions?

1. Can't find RPI IP?
   → Check NETWORK_SETUP_GUIDE.md section "Find the RPI Static IP Address"

2. Still getting [FAIL]?
   → RPI is likely powered off or on a different network
   → Verify: Power on RPI and check it's on your network

3. What if I can't access router?
   → See NETWORK_SETUP_GUIDE.md section "Method 2: Environment Variables"
   → Try SSH: `ssh <user>@phaser.local` to get IP from RPI directly

## Before You Close

✅ Run `python diagnose_hardware.py` to confirm your system setup
✅ Check NETWORK_SETUP_GUIDE.md for detailed instructions
✅ Set up config.py or environment variables
✅ Restart backend and try Find HB100 again

You've got this! 🚀

