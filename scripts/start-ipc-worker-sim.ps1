Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pythonExe = Join-Path $repoRoot '.venv\Scripts\python.exe'
$workerPath = Join-Path $repoRoot 'phaser_ipc_worker.py'

if (-not (Test-Path $pythonExe)) {
    throw "Python venv not found at $pythonExe"
}

& $pythonExe $workerPath --sim

