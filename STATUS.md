# STATUS

Paper-only real-time stock scanner and alert platform. Target market: **US stocks** (confirmed with the requester; the uploaded
resource map and master prompt are about US equities, not forex).

Last verified: 2026-09-30 on Linux, Python 3.11.15.

## Definition of done

| Item | Result | Evidence |
| --- | --- | --- |
| Installs from a clean environment | **Done** | Fresh venv from a clean copy of the tracked files: `pip install ".[dev]"`, `check-config`, `migrate`, `seed-fixtures`, `smoke` all succeeded |
| Migrations run | **Done** | `python -m scanalert migrate` applies `0001_initial.sql`; idempotent; tamper-checked (`test_db.py`) |
| Fixture data can be loaded | **Done** | `seed-fixtures` writes 62,400 bars (8 symbols, 20 sessions); server and smoke test use the CSV when present |
| Scanner deterministic on fixtures | **Done** | Two independent replays produce identical event ids (`test_fixture_replay_produces_persisted_deterministic_alerts`) |
| Alert engine emits and persists | **Done** | Alerts, suppressed alerts and deliveries persisted; verified via API, DB and WebSocket |
| UI shows live-style alerts and Top Lists | **Done** | Verified in headless Chromium: all 8 screens, forms, acknowledge, paper intent, backtest to report, mobile width (no console errors, no horizontal scroll) |
| Backtest runs and reports | **Done** | 42 engine tests with hand-computed expectations + API/e2e/smoke |
| Paper intents/fills without any broker write | **Done** | No order code exists; tests block sockets/HTTP during intent creation; DB CHECK constraints |
| Paper-only guard automated tests | **Done** | `test_safety.py` (39 tests) incl. repo scan for live URLs/credentials |
| Reconnect, duplicate, stale, correction behaviour tested | **Done** | `test_stream.py`, `test_integration.py` (fault injection: duplicates, late bar, correction, disconnect, halt) |
| PowerShell scripts | **Partially verified** | PowerShell is not installed on the build host; setup/migrate/test confirmed by a user run on Windows, the rest statically checked only (strict mode, dry-run, failure messages, paper guard). The Python commands they wrap were executed. See below |
| API documentation generated | **Done** | `/docs`, `/openapi.json`, `API.md` (examples captured from the running app) |
| README setup instructions | **Done** | `README.md`, `POWER_SHELL_SETUP.md` |
| No secrets committed | **Done** | `.env` git-ignored; `.env.example` has empty credentials (test enforces); grep for key patterns clean |
| No live endpoint, credential, order submission | **Done** | See `PAPER_TRADING_SAFETY.md` and its tests |
| Diff, tests, lint, types, smoke reviewed | **Done** | Results below |
| `STATUS.md` | **Done** | This file |

## Verification results (final run)

| Check | Command | Result |
| --- | --- | --- |
| Tests | `python -m pytest -q` | **336 passed** (about 80 s) |
| Lint | `ruff check src tests` | All checks passed |
| Format | `ruff format --check src tests` | Clean |
| Types | `mypy src` | Success: no issues in 24 source files |
| Smoke | `python -m scanalert smoke` | **ALL 20 STEPS PASSED** (real server process, temp DB, WebSocket alert, ack, paper intent, backtest, guard refusals) |
| Headless UI | Playwright + system Chromium | All screens rendered, forms exercised, 0 console/HTTP errors |

Test counts: safety 39, calendar 30, formula 25, features/filters 32, scanner/alerts 31, paper 30, backtest 42, stream/Alpaca 25,
DB 15, integration 21, API end-to-end 13, scripts/docs 33.

## Run commands

Windows PowerShell (from the repository root):

```powershell
.\scripts\setup.ps1
.\scripts\migrate.ps1
.\scripts\test.ps1 -Lint -TypeCheck
.\scripts\run-smoke-test.ps1
.\scripts\run-dev.ps1        # UI http://127.0.0.1:8000/ui/  docs http://127.0.0.1:8000/docs
.\scripts\stop-dev.ps1
```

Any shell: see `README.md` (`python -m scanalert check-config | migrate | seed-fixtures | serve | smoke`).

## Defects found and fixed during the build (by tests or review)

`new_high`/`new_low` could never fire; percentage trailing stops did not trail the high-water mark; the event buffer reordered bars ahead of
earlier ticks; the auth-failure counter reset too early; gap diagnostics used the host timezone; unsynchronised SQLite access from worker
threads crashed the interpreter in tests; delivery-delay warnings were computed after sending and fixture replay reported false delays; test
notifications appeared in the alert list; filter-value errors were confusing; redacted SQLite URLs printed `None`. Each has a regression test except the UI-only fix (holding-time bucket order).

## Known limitations and unverified items (read before relying on this)

1. **Alpaca adapter is unverified against a live feed.** The vendor documentation hosts were unreachable (egress proxy) and no data credential
   was available. Message parsing, auth handshake and subscription frames follow the documented public schema from memory and are tested only with
   scripted fake connections and an httpx mock transport. Expect to adjust field names/status handling on first contact (halt/resume detection is
   text-based). This is the main integration risk.
2. **Trade Ideas and vendor pages were not fetched.** Behaviour comes from the master prompt and `trade-ideas-technical-resource-map.md`. Nothing
   claims compatibility with Trade Ideas internals.
3. **PowerShell scripts: only partly confirmed.** A user run on Windows PowerShell 5.1 with Python 3.14 confirmed `setup.ps1` and `migrate.ps1` work and `test.ps1` runs (335 of 336 passed; the failure was a test that blocked Windows' internal loopback socket, now fixed). `run-smoke-test.ps1`, `run-dev.ps1` and `stop-dev.ps1` have not yet been confirmed on Windows. Please run the verification block in `POWER_SHELL_SETUP.md` on Windows. Known risk areas:
   `Start-Process` redirection in `run-dev.ps1`, execution policy.
4. **Synthetic data only.** All alerts, top lists and backtest numbers in this repo come from a seeded synthetic generator. The very high win rates
   in sample reports are artefacts of the synthetic scenarios and mean nothing about markets.
5. **PostgreSQL untested.** The schema/SQL avoid SQLite-only features but only SQLite was exercised.
6. **NYSE calendar** is rule-based and should be re-verified yearly against nyse.com; special closures list is maintained by hand.
7. **Backtest scope:** no parameter sweeps, walk-forward, Monte Carlo, regime splits or latency runs; no dividends/short-borrow/halts; costs default
   to zero (set them). `time_after_entry` is exact-minute (OddsMaker rounds to 5 minutes). Point-in-time constituent data is not bundled.
8. **Live-mode behaviour:** partial (in-progress) minute bars are not maintained; features update on tick/bar arrival. Open paper positions are in
   memory for the session (intents/fills persist). Ticks are not persisted by default.
9. **No authentication** on the API/UI (local use, loopback default). Add auth/TLS before any network exposure.
10. **No Docker Compose** (optional in the brief; not added to avoid replacing the native Windows path).
11. **No frontend unit-test framework** exists (UI has no build). UI behaviour is covered by API tests, the smoke test and the headless-browser check
    done during development; that browser script is not committed as a test.

## Genuine external blockers

* Validating the Alpaca adapter needs the user's **Alpaca market-data keys** (data only; never trading keys) and network access.
* Running/validating the PowerShell scripts needs **Windows PowerShell**.
* Nothing in this project requires or performs a live order; no live action is pending.

## Files

Application `src/scanalert/` (config, models, calendar, features, formula, filters, strategy, scanner, alerts, notify, paper, backtest, db,
migrations/0001_initial.sql, service, bootstrap, api, smoke, providers/{base,stream,alpaca,fixtures}, ui/{index.html,app.js,style.css});
`tests/` (12 modules); `scripts/*.ps1` (10); docs: `README.md`, `ARCHITECTURE.md`, `API.md`, `DATA_MODEL.md`, `BACKTEST_METHODOLOGY.md`,
`PAPER_TRADING_SAFETY.md`, `POWER_SHELL_SETUP.md`, `CHANGELOG.md`, `STATUS.md`, `.env.example`, `pyproject.toml`, `.gitignore`.
