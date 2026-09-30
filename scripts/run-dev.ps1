[CmdletBinding()]
param([switch]$DryRun, [switch]$NoBrowser, [int]$Port = 8000, [double]$ReplaySpeed = 30)
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

# One-command development run: migrate, seed fixtures, start the backend in the background, open the UI.
# Usage: .\scripts\run-dev.ps1 [-DryRun] [-NoBrowser] [-Port 8000] [-ReplaySpeed 30]
if ($DryRun) { $py = ".venv\Scripts\python.exe" } else { $py = Require-VenvPython }
$runDir = Join-Path $Root ".run"
if (-not $DryRun) { New-Item -ItemType Directory -Force $runDir | Out-Null }
$pidFile = Join-Path $runDir "backend.pid"
if ((Test-Path $pidFile) -and -not $DryRun) {
    $old = Get-Content $pidFile
    if (Get-Process -Id $old -ErrorAction SilentlyContinue) { throw "Backend already running (PID $old). Run .\scripts\stop-dev.ps1 first." }
}
Invoke-Step "migrating database" { & $py -m scanalert migrate }
Invoke-Step "seeding deterministic synthetic fixtures" { & $py -m scanalert seed-fixtures }
$env:FIXTURE_REPLAY_SPEED = "$ReplaySpeed"
Write-Host "[scanalert] starting backend on port $Port (fixture replay speed ${ReplaySpeed}x)"
if ($DryRun) { Write-Host "[scanalert]   (dry run: not executed)"; exit 0 }
$log = Join-Path $runDir "backend.log"
$proc = Start-Process -FilePath $py -ArgumentList @("-m", "scanalert", "serve", "--port", "$Port") -PassThru -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError (Join-Path $runDir "backend.err.log")
Set-Content $pidFile $proc.Id
$up = $false
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 500
    try { $h = Invoke-RestMethod "http://127.0.0.1:${Port}/health" -TimeoutSec 2; $up = $true; break } catch { }
}
if (-not $up) { throw "Backend did not become healthy. See $log and .run\backend.err.log" }
Write-Host "[scanalert] backend UP (PID $($proc.Id)): paper_only=$($h.paper_only). UI: http://127.0.0.1:${Port}/ui/  Docs: http://127.0.0.1:${Port}/docs"
Write-Host "[scanalert] stop with .\scripts\stop-dev.ps1"
if (-not $NoBrowser) { Start-Process "http://127.0.0.1:${Port}/ui/" }
