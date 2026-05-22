Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$frontendDir = Join-Path $repoRoot 'frontend'

if (-not (Test-Path $frontendDir)) {
    throw "Frontend directory not found: $frontendDir"
}

Push-Location $frontendDir
try {
    if (Test-Path (Join-Path $frontendDir 'package-lock.json')) {
        npm ci
    } else {
        npm install
    }
    npm run build
} finally {
    Pop-Location
}

$distDir = Join-Path $frontendDir 'dist'
if (-not (Test-Path $distDir)) {
    throw "Frontend build did not produce dist directory: $distDir"
}

Write-Host "Frontend build complete: $distDir"

