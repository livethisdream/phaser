param(
    [string]$OutputDir = 'release',
    [switch]$SkipFrontendBuild,
    [switch]$NoZip
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$outputRoot = Join-Path $repoRoot $OutputDir
$bundleDir = Join-Path $outputRoot 'PhaserBundle'
$zipPath = Join-Path $outputRoot 'PhaserBundle.zip'

if (-not $SkipFrontendBuild) {
    & (Join-Path $repoRoot 'scripts\build-frontend.ps1')
}

if (Test-Path $bundleDir) {
    Remove-Item $bundleDir -Recurse -Force
}
New-Item -ItemType Directory -Path $bundleDir | Out-Null

$filesToCopy = @(
    'ADAR_pyadi_functions.py',
    'config.py',
    'phaser_cal.py',
    'phaser_find_hb100.py',
    'phaser_functions.py',
    'phaser_server.py',
    'phaser_service.py',
    'phaser_ipc_worker.py',
    'SDR_functions.py',
    'pyproject.toml',
    'uv.lock',
    'requirements.txt',
    'README.md',
    'AGENTS.md'
)

foreach ($file in $filesToCopy) {
    $src = Join-Path $repoRoot $file
    if (Test-Path $src) {
        Copy-Item $src -Destination (Join-Path $bundleDir $file)
    }
}

$optionalCalFiles = @(
    'calibration.json',
    'hb100_cal.txt',
    'phase_cal_val.pkl',
    'gain_cal_val.pkl',
    'channel_cal_val.pkl'
)

foreach ($file in $optionalCalFiles) {
    $src = Join-Path $repoRoot $file
    if (Test-Path $src) {
        Copy-Item $src -Destination (Join-Path $bundleDir $file)
    }
}

$frontendDist = Join-Path $repoRoot 'frontend\dist'
if (-not (Test-Path $frontendDist)) {
    throw "Frontend dist not found at $frontendDist. Run scripts/build-frontend.ps1 first."
}

$bundleFrontendDir = Join-Path $bundleDir 'frontend'
New-Item -ItemType Directory -Path $bundleFrontendDir | Out-Null
Copy-Item $frontendDist -Destination (Join-Path $bundleFrontendDir 'dist') -Recurse

$bundleScriptsDir = Join-Path $bundleDir 'scripts'
New-Item -ItemType Directory -Path $bundleScriptsDir | Out-Null
Copy-Item (Join-Path $repoRoot 'scripts\install.ps1') -Destination (Join-Path $bundleScriptsDir 'install.ps1')
Copy-Item (Join-Path $repoRoot 'scripts\start-real.ps1') -Destination (Join-Path $bundleScriptsDir 'start-real.ps1')
Copy-Item (Join-Path $repoRoot 'scripts\start-sim.ps1') -Destination (Join-Path $bundleScriptsDir 'start-sim.ps1')
Copy-Item (Join-Path $repoRoot 'scripts\start-ipc-worker-real.ps1') -Destination (Join-Path $bundleScriptsDir 'start-ipc-worker-real.ps1')
Copy-Item (Join-Path $repoRoot 'scripts\start-ipc-worker-sim.ps1') -Destination (Join-Path $bundleScriptsDir 'start-ipc-worker-sim.ps1')
Copy-Item (Join-Path $repoRoot 'scripts\test-ipc-worker.ps1') -Destination (Join-Path $bundleScriptsDir 'test-ipc-worker.ps1')

if (-not $NoZip) {
    if (Test-Path $zipPath) {
        Remove-Item $zipPath -Force
    }
    Compress-Archive -Path (Join-Path $bundleDir '*') -DestinationPath $zipPath -CompressionLevel Optimal
    Write-Host "Created bundle zip: $zipPath"
}

Write-Host "Bundle directory ready: $bundleDir"
