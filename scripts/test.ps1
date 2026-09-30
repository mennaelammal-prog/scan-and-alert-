[CmdletBinding()]
param([switch]$DryRun, [switch]$Lint, [switch]$TypeCheck, [string]$Filter = "")
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

# Runs the pytest suite; -Lint adds ruff, -TypeCheck adds mypy.
# Usage: .\scripts\test.ps1 [-DryRun] [-Lint] [-TypeCheck] [-Filter "backtest"]
if ($DryRun) { $py = ".venv\Scripts\python.exe" } else { $py = Require-VenvPython }
if ($Lint) { Invoke-Step "ruff check" { & $py -m ruff check src tests } }
if ($TypeCheck) { Invoke-Step "mypy" { & $py -m mypy src } }
if ($Filter) { Invoke-Step "pytest -k $Filter" { & $py -m pytest -q -k $Filter } }
else { Invoke-Step "pytest" { & $py -m pytest -q } }
