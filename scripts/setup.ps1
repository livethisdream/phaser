# One-stop Phaser setup for Windows testers (PowerShell).
#
# This is the FIRST-TIME provisioning path: it installs Python deps and the
# systemd unit on the Pi, which deploy.py does not do. After running it once,
# `python deploy.py` is all you need for every subsequent update.
#
#   1. Verifies local prereqs (Python 3.11+, ssh, scp; node/npm only to build)
#   2. Uses the committed frontend build, or builds it if absent/-Build
#   3. ssh to the Pi and provision it (installs deps + systemd unit)
#   4. Runs deploy.py to copy files + start the service
#
# Usage:
#   .\scripts\setup.ps1                  # Pi at phaser.local (default)
#   .\scripts\setup.ps1 192.168.1.42     # Pi at a specific IP
#   .\scripts\setup.ps1 -SkipPi          # laptop side only
#   .\scripts\setup.ps1 -Build           # force a rebuild (needs Node)
#
# The Pi must be reachable and your ssh key must already be authorized
# for user `analog`. Test with:  ssh analog@<host> 'echo ok'
#
# On Windows, OpenSSH client is a Windows optional feature — install via:
#   Settings > Apps > Optional features > OpenSSH Client

param(
    [Parameter(Position=0)]
    [string]$PiHost = "phaser.local",

    [switch]$SkipPi,

    [switch]$Build
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# This script lives in scripts/ but every path below is repo-root relative,
# so anchor to the root regardless of where it was invoked from.
Set-Location (Join-Path $PSScriptRoot '..')

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

function Have-Command {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

Need-Command python

# node/npm are NOT required. frontend\dist is committed (built by CI), so the
# normal path has no toolchain at all. They are only checked if we actually
# have to build -- see the next step.

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
Write-Host "[2/4] Frontend..."

if (-not (Test-Path "frontend\dist\index.html")) {
    Write-Host "  No committed build found; it will have to be built."
    $Build = $true
}

if ($Build) {
    if (-not (Have-Command node) -or -not (Have-Command npm)) {
        Write-Host "  ERROR: building needs node + npm, which are not on PATH."
        if (Test-Path "frontend\dist\index.html") {
            Write-Host "  Drop -Build to use the committed build instead."
        } else {
            Write-Host "  Install Node, or check out a commit that has frontend\dist\."
        }
        exit 1
    }
    Write-Host "  Building beamforming frontend..."
    Push-Location frontend
    try {
        & npm install --silent
        if ($LASTEXITCODE -ne 0) { throw "npm install failed in frontend/" }
        & npm run build
        if ($LASTEXITCODE -ne 0) { throw "npm run build failed in frontend/" }
    } finally { Pop-Location }

    if (Test-Path "frontend-radar\package.json") {
        Write-Host "  Building radar frontend..."
        Push-Location frontend-radar
        try {
            & npm install --silent
            if ($LASTEXITCODE -ne 0) { throw "npm install failed in frontend-radar/" }
            & npm run build
            if ($LASTEXITCODE -ne 0) { throw "npm run build failed in frontend-radar/" }
        } finally { Pop-Location }
    }
} else {
    Write-Host "  OK: using the committed build (no Node required)"
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
Get-Content (Join-Path $PSScriptRoot 'setup-pi.sh') -Raw | & ssh -t "analog@$PiHost" 'bash -s'
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
Write-Host "  Open: http://${PiHost}:8080/"
Write-Host "  (Instructor mode: http://${PiHost}:8080/?instructor=1)"
Write-Host ""
Write-Host "  Watch logs:  ssh analog@$PiHost 'sudo journalctl -u phaser-headless -f'"
Write-Host "  Redeploy:    python deploy.py $PiHost"
Write-Host "=================================================="
