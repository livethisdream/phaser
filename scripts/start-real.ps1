Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $venvPython)) {
    throw "Python environment not found. Run scripts/install.ps1 first."
}

Push-Location $repoRoot
try {
    & $venvPython phaser_server.py @args
} finally {
    Pop-Location
}

