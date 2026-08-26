# Phaser Hardware URI Diagnostic Guide

This guide helps you determine the correct URIs for your specific Phaser hardware setup.

## Quick Diagnosis

Run this PowerShell command to see what the new code detected:

```powershell
$pythonCode = @"
import socket
if socket.gethostname().find(".") >= 0:
    hostname = socket.gethostname()
else:
    hostname = socket.gethostbyaddr(socket.gethostname())[0]

print(f"Your hostname: {hostname}")
if "phaser" in hostname:
    print("Detection: Running ON the Phaser hardware board")
    print("Should use: rpi_uri='ip:localhost', sdr_uri='ip:192.168.2.1'")
else:
    print("Detection: Running on a LAPTOP controlling remote Phaser")
    print("Should use: rpi_uri='ip:phaser.local' (or specific IP), sdr_uri='ip:phaser.local:50901'")
"@

python -c $pythonCode
```

## Scenario Matrix

| Your Setup | Hostname | Detection | Recommended URIs |
|-----------|----------|-----------|------------------|
| Phaser board with local USB/Ethernet | Contains "phaser" | On-board | `rpi_uri='ip:localhost'`<br/>`sdr_uri='ip:192.168.2.1'` |
| Laptop with SSH tunnel to hardware | Any | Laptop | Set via `config.py` or hostname resolution |
| Laptop with mDNS (hostname resolution) | Any | Laptop | `rpi_uri='ip:phaser.local'`<br/>`sdr_uri='ip:phaser.local:50901'` |
| Laptop with static IP to hardware | Any | Laptop | `rpi_uri='ip:192.168.X.X'`<br/>`sdr_uri='ip:192.168.X.X:50901'` |

## Method 1: Auto-Detection (Default, No Config Needed)

The script now automatically:
1. Detects your hostname
2. If "phaser" in hostname → uses on-board URIs
3. Otherwise → uses laptop URIs (`ip:phaser.local`)

**Pros**: Zero configuration needed for most setups  
**Cons**: Only works if mDNS is available or defaults match your setup

## Method 2: Environment Variables (Temporary)

Override for a single run without editing config files:

```powershell
# PowerShell
$env:PHASER_RPI_URI = "ip:192.168.1.100"
$env:PHASER_SDR_URI = "ip:192.168.1.100:50901"
python phaser_server.py
```

```bash
# Linux/Mac Bash
export PHASER_RPI_URI="ip:192.168.1.100"
export PHASER_SDR_URI="ip:192.168.1.100:50901"
python phaser_server.py
```

## Method 3: config.py (Persistent)

Edit `config.py` in your Phaser directory:

```python
# config.py - Laptop with context-forwarded SDR
uri_mode = "prefer_config"
rpi_uri = "ip:phaser.local"
sdr_uri = "ip:phaser.local:50901"
```

Or for static IPs:

```python
# config.py - Laptop with specific IP hardware
uri_mode = "prefer_config"
rpi_uri = "ip:192.168.1.100"
sdr_uri = "ip:192.168.1.100:50901"
```

## Troubleshooting Steps

### 1. Verify Hostname Detection
Run the diagnostic code above to see what your system reports.

### 2. Test Network Connectivity
```powershell
# Test if you can reach the hardware
ping phaser.local

# Or test specific IP if you know it
ping 192.168.1.100
```

### 3. Check Firewall/Network
- Ensure TCP ports 50901 (SDR), 8000 (web), and 8888 (IIO) are open
- If on corporate network, check if mDNS (hostname.local) resolution is allowed
- Some corporate networks block .local domain resolution

### 4. Verify Hardware is Powered
- Check that Phaser hardware is powered on
- Check that Plutone (SDR) is powered
- Check network connectivity cables

### 5. Manual Port Forwarding
If using SSH tunnel:
```powershell
# SSH tunnel example (forward remote ports to localhost)
ssh.exe -L 50901:192.168.2.1:50901 user@remote_phaser_host

# Then use
$env:PHASER_RPI_URI = "ip:localhost"
$env:PHASER_SDR_URI = "ip:localhost:50901"
```

### 6. Check if phaser.local Resolves
```powershell
# Try to resolve phaser.local
nslookup phaser.local

# Or use hostname directly from hardware
# SSH into Phaser hardware and run: hostname -I
```

## Error Message Decoding

After the fix, if connection fails, you'll see:

```
Error: Failed to connect to hardware at rpi=ip:phaser.local and sdr=ip:phaser.local:50901
Details: Cannot create IIO context [Details about the actual error]
```

The "Details:" part tells you:
- `Cannot create IIO context` → Hardware unreachable (network issue)
- `Connection refuse` → Hardware reachable but port not listening
- `Name or service not known` → Hostname doesn't resolve (DNS/mDNS issue)

## URI Format Reference

```
ip:phaser.local              # mDNS hostname (requires .local support)
ip:192.168.1.100             # Static IP
ip:phaser.local:50901        # mDNS with specific port
ip:192.168.1.100:50901       # IP with specific port
ip:localhost                 # Loopback (only on Phaser board)
ip:192.168.2.1               # Phaser board default static IP
```

## When to Use Each Method

| Situation | Best Method | Example URI |
|-----------|-------------|------------|
| Same network, device supports mDNS | Auto-detect | `ip:phaser.local` |
| Static IP configured | config.py | `ip:192.168.1.100` |
| SSH tunnel forwarded | Environment vars | `ip:localhost:50901` |
| One-time test/debug | Inline env vars | `$env:PHASER_RPI_URI = "..."` |
| On Phaser board directly | Auto-detect | `ip:localhost` |

## Still Having Issues?

1. Run the diagnostic script above to confirm hostname detection
2. Note the "Troubleshooting" steps printed when calibration fails
3. Verify network connectivity with `ping` commands
4. Check firewall settings
5. Test with static IP if mDNS isn't working
6. Consult `config.py` for all available configuration options

