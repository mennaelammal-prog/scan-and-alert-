[CmdletBinding()]
param([switch]$DryRun, [switch]$Force)
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

# Creates .venv, installs the package with dev tools and creates .env from .env.example (paper defaults).
# Usage: .\scripts\setup.ps1 [-DryRun] [-Force]
$base = Get-BasePython
if (-not (Test-Path ".venv") -or $Force) {
    $venvArgs = if ($base -eq "py") { @("-3", "-m", "venv", ".venv") } else { @("-m", "venv", ".venv") }
    Invoke-Step "creating virtual environment .venv (Python 3.11+ required)" { & $base @venvArgs }
} else { Write-Host "[scanalert] .venv already exists (use -Force to recreate)" }
if (-not (Test-Path ".env")) {
    Invoke-Step "creating .env from .env.example (TRADING_MODE=paper, DATA_PROVIDER=fixture)" { Copy-Item ".env.example" ".env"; $global:LASTEXITCODE = 0 }
} else { Write-Host "[scanalert] .env already exists; leaving it untouched" }
if (-not $DryRun) { $py = Require-VenvPython } else { $py = ".venv\Scripts\python.exe" }
Invoke-Step "installing dependencies" { & $py -m pip install --upgrade pip; & $py -m pip install -e ".[dev]" }
Invoke-Step "checking configuration (paper-only guard)" { & $py -m scanalert check-config }
Write-Host "[scanalert] setup complete. Next: .\scripts\migrate.ps1 ; .\scripts\test.ps1 ; .\scripts\run-dev.ps1"
