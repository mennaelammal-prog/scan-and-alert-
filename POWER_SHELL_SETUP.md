# PowerShell setup

All commands are run from the repository root (`scan-and-alert-`) in Windows PowerShell 5.1 or PowerShell 7. Every script:

* uses `Set-StrictMode -Version Latest` and `$ErrorActionPreference = "Stop"`;
* prints the current directory and `TRADING_MODE`, and **throws** if `TRADING_MODE` is anything other than `paper`;
* fails with a clear message when Python or the virtual environment is missing;
* never prints secrets (only the redacted `check-config` view);
* supports `-DryRun` (prints what it would do without doing it) and, where meaningful, `-Status`.

> **Testing note.** These scripts were written and statically checked on Linux, where PowerShell is not installed, so they have **not been
> executed** by the author. The Python commands they wrap (`python -m scanalert ...`) were executed and tested. Please run the verification
> block at the bottom on your Windows machine and report any script error.

If PowerShell blocks script execution, allow it for the current window only:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

## Scripts

| Script | What it does | Expected output | Verify |
| --- | --- | --- | --- |
| `scripts\setup.ps1 [-DryRun] [-Force]` | Creates `.venv`, installs `pip install -e ".[dev]"`, copies `.env.example` to `.env` if missing, runs the paper guard | `setup complete`, redacted config JSON with `"paper_only": true` | `.venv\Scripts\python.exe --version` prints 3.11+ |
| `scripts\install.ps1 [-DryRun]` | Re-installs dependencies into `.venv` | pip output | `.venv\Scripts\python.exe -m pip show scanalert` |
| `scripts\migrate.ps1 [-DryRun] [-Status]` | Applies SQL migrations to `DATABASE_URL` | `migrations applied: ['0001_initial.sql']` (or `none (up to date)`) | `data\scanalert.db` exists |
| `scripts\test.ps1 [-Lint] [-TypeCheck] [-Filter x] [-DryRun]` | pytest (+ ruff, mypy) | `N passed` | exit code 0 |
| `scripts\run-backend.ps1 [-Port 8000] [-ReplaySpeed 30] [-Status] [-DryRun]` | Runs API + UI in the foreground | uvicorn log, URLs | open http://127.0.0.1:8000/ui/ |
| `scripts\run-frontend.ps1 [-Status] [-DryRun]` | Checks the backend and opens the UI in the browser (the UI is served by the backend, there is no separate dev server) | `backend OK (paper_only=True ...)` | browser opens |
| `scripts\run-dev.ps1 [-ReplaySpeed 30] [-NoBrowser] [-DryRun]` | migrate + seed fixtures + backend in the background (PID in `.run\backend.pid`, logs in `.run\`) + opens the UI | `backend UP (PID n)` | `Invoke-RestMethod http://127.0.0.1:8000/health` |
| `scripts\stop-dev.ps1 [-Status] [-DryRun]` | Stops the background backend | `stopped` | health endpoint no longer answers |
| `scripts\seed-fixtures.ps1 [-Days 20]` | Writes deterministic synthetic bars to `data\fixtures\bars_1min.csv` | `wrote N bars for 8 symbols` | file exists |
| `scripts\run-smoke-test.ps1` | Starts a temporary server on a free port with a temp database and exercises the whole flow | `ALL 20 STEPS PASSED` | exit code 0 |

## Verification block (copy/paste as one block)

```powershell
cd <path-to>\scan-and-alert-
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\scripts\setup.ps1
.\scripts\migrate.ps1
.\scripts\test.ps1 -Lint -TypeCheck
.\scripts\run-smoke-test.ps1
.\scripts\run-dev.ps1
Invoke-RestMethod http://127.0.0.1:8000/health | ConvertTo-Json -Depth 3
.\scripts\stop-dev.ps1
```

Success looks like: `setup complete`, a passing test summary, `ALL 20 STEPS PASSED (paper-only, no broker contacted)`,
`backend UP`, a `/health` JSON with `"paper_only": true` and `"trading_mode": "paper"`, then `stopped`.

## Confirming the safety guard yourself

```powershell
$env:TRADING_MODE = "live"; .\.venv\Scripts\python.exe -m scanalert check-config; $env:TRADING_MODE = "paper"
```

Expected: `REFUSING TO START` with the violation listed and exit code 2 (`$LASTEXITCODE`).

## Common problems

| Problem | Fix |
| --- | --- |
| `py`/`python` not found | Install Python 3.11+ and open a new PowerShell window |
| Script cannot be loaded because running scripts is disabled | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` |
| `Virtual environment missing` | Run `.\scripts\setup.ps1` |
| `Backend already running (PID n)` | `.\scripts\stop-dev.ps1` |
| Port 8000 busy | `.\scripts\run-dev.ps1 -Port 8001` |
| Backend did not become healthy | Read `.run\backend.log` and `.run\backend.err.log` |
