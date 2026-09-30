# Data model

Schema source of truth: `src/scanalert/migrations/0001_initial.sql` (dialect-neutral SQL). The migration runner
(`python -m scanalert migrate`) records each applied file with a SHA-256 checksum in `schema_migrations` and refuses to
run if an applied file was edited.

Conventions: timestamps are ISO-8601 UTC strings (`2026-09-28T13:50:00.000Z`, sortable and lossless); JSON documents are
`TEXT`; booleans are `INTEGER 0/1`; prices/volumes are `DOUBLE PRECISION`. SQLite is the development database; the SQL is
written to run on PostgreSQL unchanged (`INSERT .. ON CONFLICT` upserts, no SQLite-only syntax). PostgreSQL has **not** been
exercised by this repository's tests. To use native types later, add `0002_*.sql` migrations (`timestamptz`, `jsonb`).

## Safety constraints enforced by the database

| Table.column | Constraint |
| --- | --- |
| `alert_events.paper_only` | `CHECK (paper_only = 1)` |
| `alert_events.status` | `CHECK (status IN ('working','triggered','invalidated','expired','acknowledged','suppressed'))` |
| `paper_intents.simulated` / `.submitted_to_broker` | `CHECK (simulated = 1)`, `CHECK (submitted_to_broker = 0)` |
| `paper_fills.simulated`, `backtest_runs.simulated`, `backtest_trades.simulated` | `CHECK (... = 1)` |
| foreign keys | enabled (`PRAGMA foreign_keys=ON` on SQLite) |

## Tables

| Table | Purpose | Key / notable columns | Indexes |
| --- | --- | --- | --- |
| `symbols` | Universe and lifecycle | PK `symbol`; `active`, `status` (`active`, `inactive`, `delisted`...), `listed_at`, `delisted_at` | PK |
| `market_sessions` | Calendar facts per date | PK `session_date`; `is_trading_day`, `open_utc`, `close_utc`, `early_close`, `holiday_name`, `calendar_version` | PK |
| `raw_market_events` | Raw normalised events for replay/debug | PK `event_key`; `provider`, `feed`, `kind`, `symbol`, **`source_ts`**, **`ingest_ts`**, `payload` (JSON) | `(symbol, source_ts)`, `(kind, source_ts)` |
| `quotes` | NBBO quotes | PK `(symbol, source_ts, bid, ask)`; sizes, `ingest_ts`, `provider`, `feed` | PK |
| `trades` | Trades | PK `(symbol, source_ts, trade_id, price, size)`; `conditions`, `ingest_ts` | PK |
| `bars` | 1-minute bars incl. revisions | PK `(symbol, timeframe, bar_ts, revision)`; OHLCV, `vwap`, `trade_count`, `corrected`, `ingest_ts`, `provider`, `feed`. Latest revision wins on read | `bar_ts` |
| `strategies` | Strategy head | PK `strategy_id`; `current_version`, `enabled` | PK |
| `strategy_versions` | Immutable versions | PK `(strategy_id, version)`; `spec_json` (exact config), `config_hash` | PK |
| `filters` | Filter rows per version and scope | PK `(strategy_id, strategy_version, scope, filter_id)`; `scope` = `gate` or `condition:<id>`; `field, operator, value_json, unit, session_basis, lookback, null_policy, enabled, filter_version` | PK |
| `formula_versions` | Formula text, canonical form and digest per version | PK `(strategy_id, strategy_version, formula_id)`; `expression`, `canonical`, `digest` | PK |
| `alert_events` | Every alert, including suppressed ones | PK `event_id`; strategy id+version, `condition_id`, `direction`, `event_type`, `source_ts`, `detected_ts`, `delivered_ts`, `delivery_delay_ms`, `session`, `trigger_price`, `bid`, `ask`, `spread_bps`, `status`, `status_reason`, `priority`, `dedupe_key`, `expires_at`, `stale_data`, `delayed_delivery`, `data_provider`, `data_feed`, `strategy_config_hash`, `feature_snapshot`, `filter_snapshot` (JSON incl. full config) | `source_ts`, `(symbol, source_ts)`, `(strategy_id, strategy_version, source_ts)`, `status`, `dedupe_key` |
| `alert_deliveries` | Notification audit | PK `delivery_id`; `event_id`, `channel`, `status` (`sent|failed|skipped`), `attempts`, `reason`, `error`, `source_ts`, `detected_ts`, `delivered_ts`, `latency_ms`, `kind` (`alert|test`) | `event_id` |
| `backtest_runs` | One row per run | PK `run_id`; strategy id/version, `config_hash`, `status`, `date_start/end`, `data_provider`, `data_feed`, `params_json`, `strategy_snapshot`, `report_json`, `error`, `label` | `(strategy_id, created_at)` |
| `backtest_trades` | Simulated trades | PK `(run_id, trade_no)`; FK `run_id`; entry/exit ts and prices, `quantity`, `gross_pnl`, `costs`, `net_pnl`, `exit_reason`, `event_id`, `detail_json` | PK |
| `paper_intents` | Unsubmitted intents | PK `intent_id`; `event_id`, `symbol`, `direction`, `quantity`, `entry_model`, `stop_model`, `target_model`, `time_exit_minutes`, `est_spread_bps`, `est_slippage_bps`, `max_modeled_loss`, strategy id/version, `status`, `expires_at`, `detail_json` | `event_id` |
| `paper_fills` | Simulated fills | PK `fill_id`; FK `intent_id`; `side`, `quantity`, `price`, `fill_ts`, `model`, `partial`, `status`, `reject_reason` | `intent_id` |
| `audit_log` | Who did what | PK `id`; `ts`, `actor`, `action`, `entity_type`, `entity_id`, `detail` | `ts` |
| `user_preferences` | Notification preferences | PK `user_id`; `prefs_json` | PK |
| `schema_migrations` | Migration ledger | PK `version`; `checksum`, `applied_at` | PK |

## Version and provenance preservation

* `alert_events` stores `strategy_id`, `strategy_version`, `strategy_config_hash`, the complete `filter_snapshot` (per-filter observed
  value, threshold, result and the whole strategy configuration with formula digests), and `data_provider`/`data_feed`.
* `backtest_runs` stores `strategy_snapshot`, `params_json` (every parameter), `data_provider`, `data_feed` and the report with
  the assumptions list.
* `bars`/`raw_market_events`/`quotes`/`trades` store `provider` and `feed`.

## Retention and volume notes

Quotes and trades are **not** persisted by default (volume); bars and raw session/provider events are. Extend
`Service._flush` if tick persistence is required, and add partitioning/retention on PostgreSQL.
