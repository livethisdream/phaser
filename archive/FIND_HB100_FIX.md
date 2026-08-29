# Fix for "Find HB100" Failing with "No device found"

## Problem

The `phaser_find_hb100.py` and `phaser_cal.py` scripts were failing to connect to hardware because they used a simplified URI (Uniform Resource Identifier) resolution function that:

1. **Didn't detect hostname context** - The scripts always tried the same default URIs (`ip:phaser.local`) regardless of where they were being run from
2. **Didn't support configuration modes** - Unlike the main server, they ignored the `uri_mode` config option that allows smart auto-detection
3. **Lacked helpful error messages** - When connection failed, there was no guidance on what to check or configure

## Root Cause

When running on a laptop, the scripts need to know:
- Is it running on the Phaser hardware board itself? → Use `ip:localhost`
- Is it running on a remote laptop controlling Phaser? → Use `ip:phaser.local` or another configured URI

Without detecting the hostname, the scripts could only guess, leading to "No device found" errors when the guessed URIs were wrong.

## Solution Applied

Updated both scripts to use the robust URI resolution logic from `phaser_service.py`:

### Changes Made

1. **phaser_find_hb100.py** - Added:
   - `_detect_hostname()` - Detects the current system hostname
   - `_auto_uris()` - Returns context-appropriate default URIs based on hostname
   - Enhanced `resolve_hardware_uris()` - Supports all precedence levels

2. **phaser_cal.py** - Same updates as above

3. **Error Handling** - Both scripts now provide clear troubleshooting guidance:
   ```
   Error: Failed to connect to hardware at rpi=X and sdr=Y
   Details: <actual error message>
   
   Troubleshooting:
   1. Check that Phaser hardware is powered on and accessible
   2. Verify PHASER_RPI_URI and PHASER_SDR_URI environment variables if set
   3. Check config.py/config_custom.py for correct uri_mode and URIs
   4. Test network connectivity to the hardware
   ```

## How It Works Now

The scripts now use intelligent URI resolution with this priority order:

1. **Environment Variables** (highest priority)
   - `PHASER_RPI_URI`
   - `PHASER_SDR_URI`

2. **Config Modes** (in order)
   - `uri_mode = "custom"` - Requires explicit rpi_uri/sdr_uri in config
   - `uri_mode = "prefer_config"` - Uses config values if present, auto-detects otherwise
   - `uri_mode = "auto"` (default) - Auto-detects based on hostname

3. **Auto-Detection** (lowest priority)
   - If hostname contains "phaser" → Use local board URIs
   - Otherwise → Use remote laptop URIs

## Configuration Options

### Option 1: Default (Recommended)
No changes needed - the script auto-detects your setup.

### Option 2: Explicit Configuration
Create/edit `config_custom.py`:
```python
uri_mode = "prefer_config"
rpi_uri = "ip:phaser.local"      # or "ip:192.168.1.100" etc.
sdr_uri = "ip:phaser.local:50901" # or your specific SDR URI
```

### Option 3: Environment Variables (One-time Override)
```powershell
$env:PHASER_RPI_URI = "ip:192.168.1.100"
$env:PHASER_SDR_URI = "ip:192.168.1.100:50901"
python phaser_find_hb100.py
```

## Display in GUI

When you run "Find HB100" or "Calibrate Phaser" from the GUI:

- **Success**: Calibration progress appears in the status panel
- **Error**: You now see the error message AND troubleshooting steps directly in the GUI

The error output from the subprocess is captured and displayed in the calibration status panel, so you don't need console access to see what went wrong.

## Testing

To verify the fix works:

1. Open the GUI and navigate to the Calibration section
2. Try "Find HB100" or "Calibrate Phaser"
3. If hardware is reachable: You'll see progress messages
4. If hardware is not reachable: You'll see the new error message with troubleshooting steps

## Additional Notes

- The same robust URI resolution is used in `phaser_server.py`, so the main backend and calibration scripts now use consistent logic
- Future scripts should import/use the same helper functions from `phaser_service.py` to maintain consistency

