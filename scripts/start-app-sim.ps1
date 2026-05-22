Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$venvDir = Join-Path $repoRoot '.venv-win'
$pythonGuiExe = Join-Path $venvDir 'Scripts\pythonw.exe'
$pythonCliExe = Join-Path $venvDir 'Scripts\python.exe'
$appPath   = Join-Path $repoRoot 'phaser_app.py'

$pythonExe = if (Test-Path $pythonGuiExe) { $pythonGuiExe } else { $pythonCliExe }

if (-not (Test-Path $pythonExe)) {
    throw "Python venv not found at $venvDir. Run: uv venv .venv-win --python 3.11 && UV_PROJECT_ENVIRONMENT=.venv-win uv sync"
}
if (-not (Test-Path $appPath)) {
    throw "phaser_app.py not found at $appPath"
}

& $pythonExe $appPath --sim

