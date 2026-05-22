Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pythonGuiExe = Join-Path $repoRoot '.venv\Scripts\pythonw.exe'
$pythonCliExe = Join-Path $repoRoot '.venv\Scripts\python.exe'
$appPath   = Join-Path $repoRoot 'phaser_app.py'

$pythonExe = if (Test-Path $pythonGuiExe) { $pythonGuiExe } else { $pythonCliExe }

if (-not $pythonExe) {
    throw "Python venv not found at $pythonGuiExe or $pythonCliExe. Run scripts\install.ps1 first."
}
if (-not (Test-Path $appPath)) {
    throw "phaser_app.py not found at $appPath"
}

& $pythonExe $appPath

