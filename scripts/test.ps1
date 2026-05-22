#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Run the Phaser test suite via uv.

.PARAMETER Markers
    Pytest marker expression, e.g. "unit", "integration", "hardware", "not hardware".
    Default: "not hardware" (safe for laptop without device attached).

.PARAMETER Coverage
    Emit a terminal coverage report.

.PARAMETER HardwareUri
    Override hardware URIs when running hardware tests.
    Expects "rpi_uri:sdr_uri" format, e.g. "ip:phaser.local:ip:phaser.local:50901".

.EXAMPLE
    .\scripts\test.ps1
    .\scripts\test.ps1 -Markers "unit"
    .\scripts\test.ps1 -Markers "hardware" -HardwareUri "ip:phaser.local:ip:phaser.local:50901"
    .\scripts\test.ps1 -Coverage
#>

param(
    [string]$Markers   = "not hardware",
    [switch]$Coverage,
    [string]$HardwareUri = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")). Path

# ── Hardware URI wiring ───────────────────────────────────────────────────────
if ($HardwareUri -ne "") {
    $parts = $HardwareUri -split ":"
    if ($parts.Count -ge 2) {
        $env:PHASER_RPI_URI = $parts[0] + ":" + $parts[1]
        if ($parts.Count -ge 4) {
            $env:PHASER_SDR_URI = $parts[2] + ":" + $parts[3] + ":" + $parts[4]
        }
    }
    Write-Host "Hardware URIs: RPI=$env:PHASER_RPI_URI  SDR=$env:PHASER_SDR_URI" -ForegroundColor Cyan
}

# ── Build pytest args ─────────────────────────────────────────────────────────
$pytestArgs = @("python", "-m", "pytest", "tests/", "-v")

if ($Markers -ne "") {
    $pytestArgs += @("-m", $Markers)
}

if ($Coverage) {
    $pytestArgs += @(
        "--cov=.",
        "--cov-omit=tests/*,release/*,radar/*",
        "--cov-report=term-missing"
    )
}

# ── Run ───────────────────────────────────────────────────────────────────────
Push-Location $repoRoot
try {
    Write-Host "Running: uv run $($pytestArgs -join ' ')" -ForegroundColor Cyan
    uv run @pytestArgs
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $exitCode

