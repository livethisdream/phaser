param(
    [switch]$Sim = $true
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pythonExe = Join-Path $repoRoot '.venv\Scripts\python.exe'
$workerPath = Join-Path $repoRoot 'phaser_ipc_worker.py'

if (-not (Test-Path $pythonExe)) {
    throw "Python venv not found at $pythonExe"
}
if (-not (Test-Path $workerPath)) {
    throw "Worker not found at $workerPath"
}

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $pythonExe
$argList = @($workerPath)
if ($Sim) { $argList += '--sim' }
$psi.Arguments = [string]::Join(' ', ($argList | ForEach-Object { '"' + $_ + '"' }))
$psi.WorkingDirectory = $repoRoot
$psi.UseShellExecute = $false
$psi.RedirectStandardInput = $true
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.CreateNoWindow = $true

$proc = New-Object System.Diagnostics.Process
$proc.StartInfo = $psi
$null = $proc.Start()

try {
    $ready = $proc.StandardOutput.ReadLine()
    $readyObj = $ready | ConvertFrom-Json
    Write-Host ("READY: cmd=" + $readyObj.cmd + ", sim_mode=" + $readyObj.data.sim_mode)

    $requests = @(
        @{ id = '1'; type = 'request'; cmd = 'get_state'; data = @{}; version = '1.0' },
        @{ id = '2'; type = 'request'; cmd = 'get_lab'; data = @{ lab_idx = 1 }; version = '1.0' },
        @{ id = '3'; type = 'request'; cmd = 'sweep'; data = @{ state = @{ PhaseValues = @(0); mode = 'Beam Sweep'; BW = 10 } }; version = '1.0' },
        @{ id = '4'; type = 'request'; cmd = 'get_cal_status'; data = @{}; version = '1.0' }
    )

    foreach ($req in $requests) {
        $json = ($req | ConvertTo-Json -Compress -Depth 10)
        $proc.StandardInput.WriteLine($json)
        $proc.StandardInput.Flush()

        $line = $proc.StandardOutput.ReadLine()
        $resp = $line | ConvertFrom-Json
        if ($req.cmd -eq 'sweep') {
            $count = if ($resp.data.ArrayGain) { $resp.data.ArrayGain.Count } else { 0 }
            Write-Host ("RESP: cmd=" + $req.cmd + ", status=" + $resp.status + ", gain_count=" + $count)
        }
        else {
            Write-Host ("RESP: cmd=" + $req.cmd + ", status=" + $resp.status)
        }
    }

    Write-Host "IPC worker smoke test complete."
}
finally {
    if (-not $proc.HasExited) {
        try { $proc.StandardInput.Close() } catch {}
        Start-Sleep -Milliseconds 100
        if (-not $proc.HasExited) {
            $proc.Kill()
        }
    }

    $stderr = $proc.StandardError.ReadToEnd()
    if ($stderr) {
        Write-Host "STDERR: $stderr"
    }
}
