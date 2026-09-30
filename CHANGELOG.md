# Changelog

## 0.1.0 (initial release)

Paper-only real-time stock scanner and alert platform.

### Added
* Fail-closed paper-only guard (settings, environment, `.env`), typed settings, redacted diagnostics.
* NYSE calendar (holidays, observed dates, early closes, special closures, DST), session classification.
* Provider interface; deterministic fixture replay provider with fault injection; Alpaca market-data adapter with resilient streaming
  (reconnect + resubscribe, exponential backoff, stale detection, ordered two-lane backpressure buffer, dedupe, rate-limit and
  auth-failure handling, gap and correction diagnostics).
* Feature registry (about 48 features), typed versioned filters, constrained formula language (types, units, nulls, bounds).
* Event scanner and snapshot (Top List) scanner; strategy versioning with config hashes.
* Alert state machine, dedupe/cooldown/daily cap, suppression audit, confirmation window, expiry; notification router with
  preferences, quiet hours, priorities, retries, delivery audit, delayed/stale flags.
* Paper intents and fill simulator (five entry models, partial fills, stale/liquidity rejection); simulated positions with stop/target/time exits.
* OddsMaker-style backtester: chronological 1-minute replay sharing live scanner code; intrabar policy; gap-through; costs; sizing; limits;
  point-in-time universe; complete report.
* FastAPI REST + WebSocket API with OpenAPI docs; dependency-free UI (dashboard, scanner builder, live alerts, Top Lists, alert detail,
  backtest runner and report, settings).
* SQL migrations (SQLite dev, PostgreSQL-compatible), DB-level paper-only constraints.
* PowerShell scripts, smoke test, documentation, 336 automated tests.

### Fixed during development (found by tests or review)
* `new_high`/`new_low` compared a bar's close with a window that included that same bar and could never fire.
* Percentage trailing stops used a fixed distance instead of trailing the high-water mark.
* Event buffer served bars before earlier ticks (now preserves arrival order while still dropping only ticks).
* Auth-failure counter reset on successful login, allowing endless retries on data-path auth errors.
* Gap diagnostics used the host timezone rather than Eastern time.
* Concurrent SQLite access from worker threads (store access is now serialised).
* Delivery-delay warning was computed after sending, so the payload never carried it; fixture replay falsely reported delays.
* Filter value validation produced a confusing three-part pydantic union error.
