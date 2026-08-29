# Documentation Index: Hardware Network Diagnostics

## 📋 Quick Start

**Just broken? Start here:**
1. Run: `python diagnose_hardware.py`
2. Read: `QUICK_REFERENCE.md`
3. Do: Follow the 3 steps in QUICK_REFERENCE.md section "What You Need To Do"

---

## 📚 All Documentation

### For End Users (Finding & Fixing Issues)

| Document | Read This If | Time |
|----------|-------------|------|
| **QUICK_REFERENCE.md** | You want the fast version | 3 min |
| **NETWORK_SETUP_GUIDE.md** | You need detailed step-by-step | 10 min |
| **NETWORK_FIX_SUMMARY.md** | You want context + solutions | 5 min |

### For Developers (Understanding the Changes)

| Document | Read This If | Time |
|----------|-------------|------|
| **DIAGNOSTIC_IMPROVEMENTS.md** | You want technical details | 8 min |
| **COMPLETE_SOLUTION.md** | You want the full end-to-end workflow | 10 min |

### For Project Context

| Document | Purpose |
|----------|---------|
| **README.md** | Main project documentation (updated with link to NETWORK_SETUP_GUIDE.md) |
| **AGENTS.md** | High-level project architecture |

---

## 🛠 Tools Available

### `diagnose_hardware.py`
**Purpose:** Quick hostname detection  
**Usage:** `python diagnose_hardware.py`  
**Shows:**
- Your machine hostname
- Whether you're on-board (Phaser) or remote (Laptop)
- Recommended URIs for your setup

**Output example:**
```
Hostname: HYB-Vuj0G9ONKro
[REMOTE] Running on a LAPTOP controlling remote Phaser
  Should use:
    rpi_uri='ip:phaser.local' (or specific IP)
    sdr_uri='ip:phaser.local:50901'
```

---

## 📊 What Was Fixed

### Problem: Cryptic Error
```
No device found  ❌
```

### Solution: Actionable Diagnostics
```
[OK] SDR 'phaser.local' resolved to: 192.168.86.39
[FAIL] RPI hostname resolution FAILED for 'phaser.local': [Errno 11001] getaddrinfo failed

Solutions:
  1. Get RPI static IP...
  2. Set static IP via environment variables...
  3. Verify Phaser hardware...
  4. Check if your network requires special configuration...
```

---

## 🚀 How to Use the Improvements

### Scenario 1: Run from Command Line

```powershell
# See what's wrong
python phaser_find_hb100.py

# Get diagnostic output → see what resolved and what didn't
# Follow the provided solutions
```

### Scenario 2: Run from GUI

```
GUI Button: "Find HB100"
    ↓
Backend spawns: python phaser_find_hb100.py
    ↓
Check GUI Logs Panel → see full diagnostic output
    ↓
Follow the provided solutions
```

### Scenario 3: Configure for Your Setup

```powershell
# Option A: Temporary (for testing)
$env:PHASER_RPI_URI = "ip:192.168.1.100"
$env:PHASER_SDR_URI = "ip:192.168.86.39"
python phaser_find_hb100.py

# Option B: Permanent (create config_custom.py)
# See NETWORK_SETUP_GUIDE.md for file content
```

---

## 📝 Documentation Levels

### Level 1: Super Quick (< 2 min)
- Run: `python diagnose_hardware.py`
- Check output

### Level 2: Quick Reference (< 5 min)
- Read: `QUICK_REFERENCE.md`
- Follow 3 steps

### Level 3: Complete Guide (< 15 min)
- Read: `NETWORK_SETUP_GUIDE.md`
- Find RPI IP
- Configure

### Level 4: Technical Details (< 20 min)
- Read: `DIAGNOSTIC_IMPROVEMENTS.md` + `COMPLETE_SOLUTION.md`
- Understand the implementation

---

## 🎯 Decision Tree

```
Do you know your RPI static IP?
  │
  ├─ YES → Go to QUICK_REFERENCE.md Step 2
  │
  └─ NO → Go to NETWORK_SETUP_GUIDE.md Section "Find the RPI Static IP Address"
           ↓
           Found it? → Go to QUICK_REFERENCE.md Step 2
           Still stuck? → Check NETWORK_SETUP_GUIDE.md "Troubleshooting"
```

---

## 🔍 Different Ways to Find RPI IP

From NETWORK_SETUP_GUIDE.md, you can:

1. **Check Router Admin Panel**
   - Open 192.168.1.1 (or your router admin URL)
   - Look for device named "phaser"
   - Note the IP

2. **Use Network Scanning Tool**
   - Scan for port 22 (SSH) or 8000 (web UI)
   - Find device with those ports

3. **SSH to Device**  
   - `ssh user@phaser.local`
   - Run `hostname -I`
   - That's your static IP

4. **Ask Network Admin**
   - They can check DHCP list
   - Or tell you the static IP assigned

---

## ✅ Verification

After setting up, verify with:

```powershell
# Quick check
python diagnose_hardware.py

# Full check
python phaser_find_hb100.py

# Both should now show [OK] for all hostnames
```

---

## 📖 Maps to Original Problem

**Original Issue:**
> "Find HB100 fails with No device found at line 70 of phaser_find_hb100.py"

**Root Cause Found:**
> RPI hostname 'phaser.local' not resolving (mDNS issue)

**Solution Provided:**
> Use static IP instead + detailed diagnostics to guide user

**Result:**
> Clear actionable output → user can fix it themselves

---

## 🎓 Learning Path

### I just want it working (Show me the money!)
1. `QUICK_REFERENCE.md` → Do the 3 steps
2. Hope SDR is at same IP as reported
3. Restart backend and try

### I want to understand what's happening
1. `NETWORK_SETUP_GUIDE.md` → Understand the setup
2. `DIAGNOSTIC_IMPROVEMENTS.md` → See what code changed
3. `COMPLETE_SOLUTION.md` → Understand end-to-end

### I want to maintain this system
1. `DIAGNOSTIC_IMPROVEMENTS.md` → Technical implementation
2. Check `phaser_find_hb100.py` → See the `_test_hostname_resolution()` function
3. Check `phaser_service.py` → Understand subprocess output capture

---

## 💡 Pro Tips

1. **Save your config:** Create `config_custom.py` with your RPI IP so it persists across restarts

2. **Check network first:** Before blaming the software, verify:
   - `ping 192.168.86.39` (SDR)
   - `ping 192.168.1.100` (your RPI IP)

3. **Watch the logs:** GUI logs panel now shows EVERYTHING, so check there first when something fails

4. **Hostname vs IP:** If mDNS fails but static IP works, use the IP in your config

5. **Environment variables:** Useful for one-off testing without modifying files

---

## 🔗 File Relationships

```
README.md
  └─ Points to NETWORK_SETUP_GUIDE.md

phaser_find_hb100.py / phaser_cal.py
  └─ Run with diagnostics
  └─ Output captured by phaser_service.py
  └─ Displayed in GUI logs panel

diagnose_hardware.py
  └─ Quick hostname detection
  └─ Helps user understand their setup

QUICK_REFERENCE.md → NETWORK_SETUP_GUIDE.md → DIAGNOSTIC_IMPROVEMENTS.md
   (Quick)              (Detailed)              (Technical)
```

---

## 📞 Questions?

| Question | Answer Location |
|----------|-----------------|
| "Where do I find RPI IP?" | NETWORK_SETUP_GUIDE.md Section 1 |
| "How do I configure it?" | QUICK_REFERENCE.md Section "What You Need To Do" |
| "Why did this happen?" | DIAGNOSTIC_IMPROVEMENTS.md or NETWORK_FIX_SUMMARY.md |
| "How does it work?" | COMPLETE_SOLUTION.md section "The Complete Workflow" |
| "What changed in code?" | DIAGNOSTIC_IMPROVEMENTS.md section "Files Modified" |

---

## ✨ Summary

You now have:
- ✅ Clear diagnostic output
- ✅ Multiple tools to help debug
- ✅ Comprehensive documentation at all levels
- ✅ Clear next steps when something fails
- ✅ Full output captured in GUI logs

**The problem is solved!** 🎉

