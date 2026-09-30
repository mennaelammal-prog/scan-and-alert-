[CmdletBinding()]
param([switch]$DryRun, [switch]$Status)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Run from anywhere: always operate in the repository root.
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
Write-Host "[scanalert] directory: $(Get-Location)"

# PAPER ONLY. Refuse anything but paper mode; this project has no live trading mode at all.
if (-not $env:TRADING_MODE) { $env:TRADING_MODE = "paper" }
if ($env:TRADING_MODE -ne "paper") { throw "TRADING_MODE must be paper (got a different value). This project has no live mode." }
Write-Host "[scanalert] mode: TRADING_MODE=$($env:TRADING_MODE) -> PAPER ONLY / SIMULATION ONLY"

function Get-VenvPython {
    $venv = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path $venv) { return $venv }
    return $null
}
function Get-BasePython {
    foreach ($c in @("py", "python", "python3")) {
        $cmd = Get-Command $c -ErrorAction SilentlyContinue
        if ($cmd) { return $c }
    }
    throw "Python 3.11+ was not found on PATH. Install it from https://www.python.org/downloads/ and re-run."
}
function Require-VenvPython {
    $py = Get-VenvPython
    if (-not $py) { throw "Virtual environment missing. Run .\scripts\setup.ps1 first." }
    return $py
}
function Invoke-Step([string]$Label, [scriptblock]$Action) {
    Write-Host "[scanalert] $Label"
    if ($DryRun) { Write-Host "[scanalert]   (dry run: not executed)"; return }
    $global:LASTEXITCODE = 0   # StrictMode: the variable must exist before it is read
    & $Action
    if ($LASTEXITCODE -ne 0) { throw "Step failed ($Label), exit code $LASTEXITCODE" }
}

# Stops the background backend started by run-dev.ps1.
# Usage: .\scripts\stop-dev.ps1 [-DryRun] [-Status]
$pidFile = Join-Path $Root ".run\backend.pid"
if (-not (Test-Path $pidFile)) { Write-Host "[scanalert] no PID file: nothing to stop"; exit 0 }
$id = [int](Get-Content $pidFile)
$p = Get-Process -Id $id -ErrorAction SilentlyContinue
if (-not $p) { Write-Host "[scanalert] PID $id is not running"; if (-not ($Status -or $DryRun)) { Remove-Item $pidFile }; exit 0 }
if ($Status) { Write-Host "[scanalert] backend running (PID $id)"; exit 0 }
Invoke-Step "stopping backend PID $id" { Stop-Process -Id $id -Force; $global:LASTEXITCODE = 0 }
if (-not $DryRun) { Remove-Item $pidFile; Write-Host "[scanalert] stopped" }
