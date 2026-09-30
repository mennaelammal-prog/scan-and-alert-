# scanalert: paper-only real-time stock scanner and alert platform

> **PAPER ONLY / SIMULATION ONLY.** This project scans US-stock market data, raises alerts, builds *hypothetical*
> paper entries with *simulated* fills, and runs *historical* backtests. It has **no live-trading mode**, no
> broker order client and no order endpoint. Startup **fails closed** if a live broker URL, live credential or
> live-trading flag is configured. See [PAPER_TRADING_SAFETY.md](PAPER_TRADING_SAFETY.md).

The behaviour is *inspired by the documented concepts* of Trade Ideas (event-driven Alert Windows, snapshot Top
Lists, typed filters and formulas, OddsMaker-style 1-minute backtests). It does not scrape Trade Ideas, use any
undocumented API, or claim compatibility with proprietary internals.

## What you get

| Area | Implementation |
| --- | --- |
| Market data | Provider interface; deterministic **fixture** replay (offline, default) and an **Alpaca market-data** adapter (WebSocket + REST, data keys only) with reconnect/resubscribe, stale detection, backpressure, dedupe, corrections and gap diagnostics |
| Sessions | US/Eastern, 09:30-16:00 regular session, NYSE holidays / early closes / special closures (rule-based calendar, not "weekdays"), premarket/postmarket flags off by default |
| Filters/formulas | Typed, versioned `FilterSpec`; constrained formula language (parser + type/unit checker + evaluator, **no `eval`**) |
| Scanners | Event scanner (false to true transitions, cooldown, dedupe) and separate snapshot scanner (ranked Top List) |
| Alerts | State machine `working/triggered/invalidated/expired/acknowledged/suppressed`, persistence, audit of suppressed signals, priorities, quiet hours, retries, delivery audit, browser (WebSocket) notifications, disabled email/webhook abstractions |
| Paper module | Unsubmitted intents (risk-based sizing, stop/target/time exit) and a fill simulator (next trade, next bar open, bid/ask cross, fixed slippage, spread+slippage, partial fills, stale/liquidity rejection) |
| Backtester | Chronological 1-minute event replay using the *same* scanner code, explicit intrabar policy, costs, sizing, limits, full report |
| UI | Dashboard, scanner builder, live alerts, Top Lists, alert detail with chart and paper proposal, backtest runner and report, settings |
| Storage | SQL migrations, portable to PostgreSQL; SQLite development default |

## Requirements

* Python **3.11+** (tested on 3.11). No Node.js is needed: the UI is dependency-free static files served by the backend.
* Windows PowerShell 5.1+ or PowerShell 7 for the `scripts\*.ps1` helpers (optional; every command also works from any shell).

## Quick start (Windows PowerShell)

Run everything from the repository root (`scan-and-alert-`).

```powershell
.\scripts\setup.ps1          # creates .venv, installs deps, creates .env (paper defaults)
.\scripts\migrate.ps1        # applies database migrations
.\scripts\test.ps1           # runs the test suite (add -Lint -TypeCheck for ruff + mypy)
.\scripts\run-dev.ps1        # migrate + seed fixtures + start backend in background + open the UI
.\scripts\stop-dev.ps1       # stop the background backend
```

Every script accepts `-DryRun` (prints the steps without running them) and prints the current directory and
`TRADING_MODE`. Details: [POWER_SHELL_SETUP.md](POWER_SHELL_SETUP.md).

## Quick start (any shell)

```bash
python -m venv .venv
. .venv/bin/activate                  # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
cp .env.example .env                  # Windows: Copy-Item .env.example .env
python -m scanalert check-config      # runs the paper-only guard, prints redacted settings
python -m scanalert migrate           # apply migrations (SQLite at data/scanalert.db by default)
python -m scanalert seed-fixtures     # optional: write deterministic synthetic bars to data/fixtures
python -m pytest -q                   # tests
FIXTURE_REPLAY_SPEED=30 python -m scanalert serve     # http://127.0.0.1:8000/ui/
python -m scanalert smoke             # end-to-end smoke test against a real temporary server
```

## URLs and ports

| What | URL |
| --- | --- |
| UI | http://127.0.0.1:8000/ui/ |
| Interactive API docs (OpenAPI) | http://127.0.0.1:8000/docs |
| OpenAPI JSON | http://127.0.0.1:8000/openapi.json |
| Health | http://127.0.0.1:8000/health |
| WebSockets | `ws://127.0.0.1:8000/ws/alerts`, `ws://127.0.0.1:8000/ws/market-status` |

Change the port with `PORT=` in `.env`, `python -m scanalert serve --port N` or `-Port N` on the scripts.

## Using the fixture replay

With `DATA_PROVIDER=fixture` (default) the app replays the *last day* of a deterministic synthetic dataset
(20 sessions, 8 scenario symbols) as a live-style stream and uses the earlier days as history for relative volume,
previous close and backtests.

* `FIXTURE_REPLAY_SPEED=0` replays instantly; `30` replays at 30x (one session in about 13 s); `1` is real time.
* Dashboard buttons **Restart replay** and **Run to end** control the replay; `POST /api/dev/replay/run?until_minutes=30`
  pauses at 10:00 ET (used by tests and the smoke test).
* Scenario symbols: `ORBX` (opening-range breakouts), `GAPU` (gap and go), `TRND`, `FLAT`, `WIDE` (150 bps spread),
  `SPKE` (volume spike), `DRFT`, `LOWV` (sub-$2, illiquid). The data is **synthetic**: it demonstrates the mechanics,
  it says nothing about real markets.

## Using real market data (Alpaca, data only)

1. Create *market data* API keys (not trading keys). Put them in `.env` as `ALPACA_DATA_KEY_ID` and
   `ALPACA_DATA_SECRET_KEY`; set `DATA_PROVIDER=alpaca` and `ALPACA_DATA_FEED=iex` (free) or `sip` (entitled).
2. `python -m scanalert check-config` then start the server. Secrets are never logged or displayed.
3. The adapter only talks to `stream.data.alpaca.markets` / `data.alpaca.markets`. Any other host, and any live
   trading host, is refused at startup. **The stream schema was implemented from the public documentation and has
   not been verified against a live feed in this repository** (no credentials/egress here); see [STATUS.md](STATUS.md).

## Security notes

* The API and UI have **no authentication** and are meant for local use: `HOST` defaults to `127.0.0.1`. Do not bind it to a public interface
  without adding authentication and TLS in front of it.
* The `/api/dev/*` endpoints (fixture replay control) exist for development and return 409 with real providers.
* Secrets come only from the environment / `.env` (git-ignored) and are redacted everywhere they could be displayed. Webhook URLs come from
  configuration, not from API input, and are checked against the broker-host list.
* All UI output is HTML-escaped; formulas are parsed, never executed.

## Configuration

All settings are typed (`src/scanalert/config.py`) and documented in [.env.example](.env.example). Notable ones:

`TRADING_MODE=paper` (only valid value), `DATA_PROVIDER`, `DATABASE_URL`, `UNIVERSE`, `ENABLE_PREMARKET` /
`ENABLE_POSTMARKET` (off), `STALE_FEED_SECONDS`, `DELAYED_DELIVERY_MS`, `TOP_LIST_REFRESH_SECONDS` (30),
`TOP_LIST_MAX_ROWS` (100), `EMAIL_ENABLED` / `WEBHOOK_ENABLED` (off).

PostgreSQL: set `DATABASE_URL=postgresql+psycopg://user:pass@host/db` and install a driver (`pip install "psycopg[binary]"`).
The migration SQL is dialect-neutral; PostgreSQL was **not** exercised in this repository's tests (SQLite only).

## Project layout

```
src/scanalert/   config, models, calendar, features, formula, filters, strategy, scanner, alerts, notify,
                 paper, backtest, db (+migrations/), service, api, providers/, ui/
tests/           pytest suite (unit, integration, end-to-end)
scripts/         PowerShell helpers
docs             ARCHITECTURE, API, DATA_MODEL, BACKTEST_METHODOLOGY, PAPER_TRADING_SAFETY, POWER_SHELL_SETUP, STATUS
```

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `REFUSING TO START: ... paper-only guard` | Read the listed violations. Remove any variable named like `*LIVE*` with a value, any `TRADING_MODE` other than `paper`, and any live broker URL from the environment and `.env`. This is intentional and cannot be overridden. |
| `Virtual environment missing` | Run `.\scripts\setup.ps1`. |
| `Python 3.11+ was not found` | Install Python 3.11+ and reopen the shell so PATH refreshes. |
| Port already in use | Use another port (`-Port 8001`) or `.\scripts\stop-dev.ps1`. |
| UI shows `REPLAY COMPLETE` and no live alerts | The fixture replay finished. Click **Restart replay** on the dashboard (speed 30 is nicer than 0). |
| `STALE FEED` badge | No market data arrived for `STALE_FEED_SECONDS` during the regular session. With Alpaca check credentials/entitlement and the Settings page counters. |
| `alpaca` provider fails to start | `ALPACA_DATA_KEY_ID`/`ALPACA_DATA_SECRET_KEY` missing, or repeated authentication failure (the supervisor stops retrying after 3 and reports it in `/health`). |
| Backtest shows 0 trades | Check the report's "Skipped signals", the entry window (default 09:35-15:30 ET) and that the date range has sessions with data (coverage table). |
| `database is locked` | Only one server process should use a SQLite file; stop other instances. |
| PowerShell blocks scripts | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` for the current window only. |

## Documentation

[ARCHITECTURE.md](ARCHITECTURE.md), [API.md](API.md), [DATA_MODEL.md](DATA_MODEL.md),
[BACKTEST_METHODOLOGY.md](BACKTEST_METHODOLOGY.md), [PAPER_TRADING_SAFETY.md](PAPER_TRADING_SAFETY.md),
[POWER_SHELL_SETUP.md](POWER_SHELL_SETUP.md), [STATUS.md](STATUS.md), [CHANGELOG.md](CHANGELOG.md).

## Sources used as behavioural references

Trade Ideas documentation (behavioural benchmark only): [Alert Window](https://www.trade-ideas.com/guide/chapter/9/9Alert_Window.html),
[Top List Window](https://www.trade-ideas.com/guide/chapter/10/10Top_List_Window.html),
[Backtesting/OddsMaker](https://www.trade-ideas.com/guide/chapter/22/22Backtesting_Oddsmaker.html); market data and paper
environments: [Alpaca real-time stock data](https://docs.alpaca.markets/docs/real-time-stock-pricing-data),
[NYSE hours and calendars](https://www.nyse.com/markets/hours-calendars), [Massive API docs](https://massive.com/docs/).
The full list is in [ARCHITECTURE.md](ARCHITECTURE.md#sources).

## Disclaimer

Educational/engineering software. Nothing here is investment advice. Alerts are hypothetical signals, paper fills are
modelled, and backtests are historical simulations that neither predict nor guarantee future results.
