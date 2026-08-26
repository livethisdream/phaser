# One-stop Phaser setup for Windows testers (PowerShell).
#
# From a fresh clone, this runs zero-to-working:
#   1. Verifies local prereqs (Python 3.11+, node, npm, ssh, scp)
#   2. npm install + npm run build for the beamforming frontend
#   3. (optional) same for the radar frontend
#   4. ssh to the Pi and provision it (installs deps + systemd unit)
#   5. Runs deploy.py to copy files + start the service
#
# Usage:
#   .\setup.ps1                       # Pi at phaser.local (default)
#   .\setup.ps1 192.168.1.42          # Pi at a specific IP
#   .\setup.ps1 -SkipPi               # laptop side only (build frontends)
#
# The Pi must be reachable and your ssh key must already be authorized
# for user `analog`. Test with:  ssh analog@<host> 'echo ok'
#
# On Windows, OpenSSH client is a Windows optional feature — install via:
#   Settings > Apps > Optional features > OpenSSH Client

param(
    [Parameter(Position=0)]
    [string]$PiHost = "phaser.local",

    [switch]$SkipPi
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Write-Host "=================================================="
Write-Host "  Phaser one-stop setup"
Write-Host "  Pi target: $PiHost"
if ($SkipPi) { Write-Host "  (-SkipPi: local build only)" }
Write-Host "=================================================="

# ---- prereqs -----------------------------------------------------------------
Write-Host ""
Write-Host "[1/4] Checking local prerequisites..."

function Need-Command {
    param([string]$Name)
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $cmd) {
        Write-Host "  ERROR: '$Name' not found on PATH. Please install it and re-run."
        exit 1
    }
    Write-Host "  OK: $Name -> $($cmd.Source)"
}

Need-Command python
Need-Command node
Need-Command npm

# ssh + scp are required when we don't -SkipPi
if (-not $SkipPi) {
    Need-Command ssh
    Need-Command scp
}

# Python >= 3.11
$pyver = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
$major, $minor = $pyver.Split('.') | ForEach-Object { [int]$_ }
if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 11)) {
    Write-Host "  ERROR: Python 3.11+ required, found $pyver"
    exit 1
}
Write-Host "  OK: python is $pyver"

# ---- frontend builds ---------------------------------------------------------
Write-Host ""
Write-Host "[2/4] Building frontend(s)..."

if ((Test-Path "frontend\package.json")) {
    Write-Host "  Building beamforming frontend..."
    Push-Location frontend
    try {
        & npm install --silent
        if ($LASTEXITCODE -ne 0) { throw "npm install failed in frontend/" }
        & npm run build
        if ($LASTEXITCODE -ne 0) { throw "npm run build failed in frontend/" }
    } finally { Pop-Location }
} else {
    Write-Host "  ERROR: frontend\package.json missing. Are you at the repo root?"
    exit 1
}

if ((Test-Path "frontend-radar\package.json")) {
    Write-Host "  Building radar frontend..."
    Push-Location frontend-radar
    try {
        & npm install --silent
        if ($LASTEXITCODE -ne 0) { throw "npm install failed in frontend-radar/" }
        & npm run build
        if ($LASTEXITCODE -ne 0) { throw "npm run build failed in frontend-radar/" }
    } finally { Pop-Location }
} else {
    Write-Host "  (skip: no frontend-radar/ - radar UI won't be available)"
}

if ($SkipPi) {
    Write-Host ""
    Write-Host "=================================================="
    Write-Host "  Local build complete. -SkipPi was set, so"
    Write-Host "  the Pi was not provisioned or deployed to."
    Write-Host "=================================================="
    exit 0
}

# ---- ssh sanity check --------------------------------------------------------
Write-Host ""
Write-Host "[3/4] Provisioning Pi at analog@$PiHost..."

# Non-interactive probe: BatchMode disables password prompts. Silent success = key works.
& ssh -o BatchMode=yes -o ConnectTimeout=5 "analog@$PiHost" 'echo ok' 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ERROR: cannot ssh to analog@$PiHost without a password."
    Write-Host "  Fix: copy your ssh key to the Pi. From PowerShell, e.g.:"
    Write-Host "    type `$HOME\.ssh\id_ed25519.pub | ssh analog@$PiHost 'mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys'"
    exit 1
}
Write-Host "  OK: passwordless ssh to analog@$PiHost works"

# Pipe setup-pi.sh over ssh. sudo prompts on the Pi go through ssh -t.
Get-Content .\setup-pi.sh -Raw | & ssh -t "analog@$PiHost" 'bash -s'
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ERROR: Pi provisioning failed (exit $LASTEXITCODE)."
    exit 1
}

# ---- deploy ------------------------------------------------------------------
Write-Host ""
Write-Host "[4/4] Deploying files..."
& python deploy.py $PiHost
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ERROR: deploy.py failed (exit $LASTEXITCODE)."
    exit 1
}

Write-Host ""
Write-Host "=================================================="
Write-Host "  Setup complete."
Write-Host "  Open: http://${Host}:8080/"
Write-Host "  (Instructor mode: http://${Host}:8080/?instructor=1)"
Write-Host ""
Write-Host "  Watch logs:  ssh analog@$PiHost 'sudo journalctl -u phaser-headless -f'"
Write-Host "  Redeploy:    python deploy.py $PiHost"
Write-Host "=================================================="
