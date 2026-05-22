param(
    [ValidateSet('pip', 'uv')]
    [string]$Installer = 'pip',
    [switch]$SkipFrontend
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command,
        [Parameter(Mandatory = $false)]
        [string[]]$Arguments = @()
    )

    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed: $Command $($Arguments -join ' ')"
    }
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $repoRoot
try {
    if ($Installer -eq 'uv') {
        Invoke-Checked -Command 'uv' -Arguments @('sync')
    } else {
        $venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
        if (-not (Test-Path $venvPython)) {
            Invoke-Checked -Command 'python' -Arguments @('-m', 'venv', '.venv')
        }
        Invoke-Checked -Command $venvPython -Arguments @('-m', 'ensurepip', '--upgrade')
        Invoke-Checked -Command $venvPython -Arguments @('-m', 'pip', 'install', '--upgrade', 'pip')
        Invoke-Checked -Command $venvPython -Arguments @('-m', 'pip', 'install', '-r', 'requirements.txt')
    }

    if (-not $SkipFrontend) {
        & (Join-Path $repoRoot 'scripts\build-frontend.ps1')
    }
} finally {
    Pop-Location
}

Write-Host "Install complete."

