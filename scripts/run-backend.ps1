[CmdletBinding()]
param([switch]$DryRun, [switch]$Status, [string]$BindHost = "127.0.0.1", [int]$Port = 8000, [double]$ReplaySpeed = -1)
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

# Starts the API + UI (foreground). Provider defaults to the offline fixture replay.
# Usage: .\scripts\run-backend.ps1 [-DryRun] [-Status] [-Port 8000] [-ReplaySpeed 30]
if ($DryRun) { $py = ".venv\Scripts\python.exe" } else { $py = Require-VenvPython }
if ($ReplaySpeed -ge 0) { $env:FIXTURE_REPLAY_SPEED = "$ReplaySpeed" }
if ($Status) {
    Invoke-Step "showing redacted configuration" { & $py -m scanalert check-config }
    try { $h = Invoke-RestMethod "http://${BindHost}:${Port}/health" -TimeoutSec 3; Write-Host "[scanalert] backend is UP: feed_state=$($h.feed_state) paper_only=$($h.paper_only)" }
    catch { Write-Host "[scanalert] backend is not running on port $Port" }
    exit 0
}
Write-Host "[scanalert] UI: http://${BindHost}:${Port}/ui/   API docs: http://${BindHost}:${Port}/docs   (Ctrl+C to stop)"
Invoke-Step "starting backend" { & $py -m scanalert serve --host $BindHost --port $Port }
