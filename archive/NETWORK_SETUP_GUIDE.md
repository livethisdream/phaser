# Phaser Hardware Network Configuration Guide

## Your Current Situation

Based on the diagnostic script, we found:

- ✅ **PlutoSDR is reachable** at IP address: `192.168.86.39`
- ❌ **Phaser RPI hostname `phaser.local` is NOT resolving**

This means:
1. The PlutoSDR device is on your network and discoverable via mDNS
2. The RPI (Phaser control board) is either offline, on a different network, or not advertising via mDNS
3. You need to find the RPI's static IP address and configure the scripts to use it

## Next Steps to Fix This

### Step 1: Find the RPI Static IP Address

#### Option A: Check Your Router's Connected Devices
1. Log into your router's admin panel (usually 192.168.1.1 or 192.168.0.1)
2. Look for connected devices named "phaser" or with hostname containing "rpi"
3. Note the IP address (example: `192.168.1.XXX`)

#### Option B: Use Your Network Scanning Tool
If you have a network scanning tool, search for devices on port 22 (SSH) or port 8000 (web UI)

#### Option C: SSH to the Device
If you have SSH access to the RPI from another device:
```bash
# From another machine that can reach the RPI
ssh user@phaser.local  # or by IP if you know it
hostname -I            # Get the RPI's IP address
```

### Step 2: Configure the Scripts with the RPI IP

Once you find the RPI IP address (let's say it's `192.168.1.100`), you have two options:

#### Option A: Use Environment Variables (Temporary)
```powershell
# In PowerShell, before running the script:
$env:PHASER_RPI_URI = "ip:192.168.1.100"
$env:PHASER_SDR_URI = "ip:192.168.86.39"
python phaser_find_hb100.py
```

#### Option B: Edit config.py (Permanent)
Edit `config.py` in the Phaser directory:

```python
# config.py
uri_mode = "prefer_config"
rpi_uri = "ip:192.168.1.100"      # Replace with actual RPI IP
sdr_uri = "ip:192.168.86.39"      # Keep this from the diagnostics
```

Then run the script:
```powershell
python phaser_find_hb100.py
```

### Step 3: Verify the Configuration Works

Test with:
```powershell
# Test hostname resolution (just the diagnostic script)
python diagnose_hardware.py
```

This should now show:
```
[REMOTE] Running on a LAPTOP controlling remote Phaser

--- Network Diagnostics ---
[OK] SDR 'phaser.local' resolved to: 192.168.86.39
[OK] RPI 'phaser.local' resolved to: 192.168.1.100
```

## Troubleshooting

### "Still can't resolve phaser.local RPI"
- The RPI may be offline
- Check if it's powered on and connected to the network
- Try pinging the IP address you found: `ping 192.168.1.100`

### "SDR IP keeps changing (wasn't 192.168.86.39 last time)"
- This is normal with mDNS; devices get assigned temporary IP addresses
- Use the IP shown in each diagnostic run, or configure a static IP on the hardware itself

### "I don't know my router admin password"
- Check the label on your router (usually printed on the device)
- Try the manufacturer defaults (often `admin` / `admin` or `admin` / empty password)

### "Port 50901 is still not responding"
Even if hostname resolves, you might get "connection refused" if:
1. The IIO daemon isn't running on the RPI
   - SSH and check: `systemctl status iiod` (must show "active")
   - Start if needed: `systemctl start iiod`

2. The RPI firewall is blocking port 50901
   - Check SSH (port 22) works first
   - Whitelist port 50901 if needed

## What These IPs Mean

| Component | IP Shown | Purpose |
|-----------|----------|---------|
| PlutoSDR | `192.168.86.39` | SDR device (Rx/Tx RF) |
| RPI Phaser | `192.168.1.100` | Control board (ADAR, LO, GPIO) |

Both are needed for full operation:
- **SDR URI** points to the PlutoSDR's context-forwarded IIO port (50901)
- **RPI URI** points to the main control board's IIO daemon

## After Fixing

Once you have the RPI IP:
1. Update `config.py` with the IPs
2. Restart the phaser_server.py backend
3. The GUI should now work normally
4. You can trigger `Find HB100` and `Calibrate` from the GUI

## Environment Variables Reference

| Variable | Example | Purpose |
|----------|---------|---------|
| `PHASER_RPI_URI` | `ip:192.168.1.100` | RPI control board |
| `PHASER_SDR_URI` | `ip:192.168.86.39:50901` | PlutoSDR Rx/Tx |

You can set these in PowerShell:
```powershell
$env:PHASER_RPI_URI = "ip:192.168.1.100"
$env:PHASER_SDR_URI = "ip:192.168.86.39:50901"
```

Or permanently in `config.py`:
```python
uri_mode = "prefer_config"  # Use config file over auto-detection
rpi_uri = "ip:192.168.1.100"
sdr_uri = "ip:192.168.86.39:50901"
```

## Questions?

If you're still stuck:
1. Run `python diagnose_hardware.py` and share the output
2. Check `phaser_server.log` in the Phaser directory for error messages
3. Verify both devices are reachable with ping tests first

