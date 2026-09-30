# Paper-trading safety

This system is **paper-only and simulation-only**. It cannot place, modify, cancel, transmit, sign or submit a financial
order, because no code path exists to do so.

## Guarantees and how they are enforced

| Guarantee | Enforcement | Tests |
| --- | --- | --- |
| Default and only mode is `paper` | `Settings.trading_mode` is `Literal["paper"]`; any other value fails validation and is reported as a guard violation | `test_safety.py` |
| Startup refuses live endpoints | `assert_paper_only` scans settings **and** the process environment **and** the `.env` file; hostnames on a live-broker list (Alpaca live/broker, TradeStation live, IBKR web API, Tradier, Schwab, OANDA live, etc.) and IBKR live gateway ports on loopback (7496, 4001) are rejected. Matching is on the parsed hostname, not substrings | `test_live_broker_urls_detected`, `test_environment_scan_flags_live_indicators` |
| Startup refuses live credentials/flags | Any variable whose name has a `LIVE` segment with a non-empty value (e.g. `ALPACA_LIVE_KEY_ID`, `ENABLE_LIVE_TRADING=true`) | same |
| Data adapters only reach data hosts | `validate_data_url` allows only `stream.data.alpaca.markets`, `data.alpaca.markets`, `api.massive.com` and loopback | `test_data_url_allowlist`, `test_alpaca_refuses_non_data_hosts` |
| Paper and live credentials are separate | Data credentials use the names `ALPACA_DATA_KEY_ID` / `ALPACA_DATA_SECRET_KEY` (never the SDK's `APCA_*` trading names); no trading credential setting exists | `.env.example`, `test_env_example_has_only_safe_placeholders` |
| No live-trading escape hatch | There is no override flag, no `--force`, no second mode. The guard runs from `load_settings()`, which every entry point uses | `test_app_refuses_to_start_with_live_config` |
| No order code | No broker SDK, no order route, no order HTTP call in `src/` | `test_source_has_no_order_submission_code`, `test_api_exposes_no_order_route` |
| Repository stays clean | A test fails if any file contains a live broker URL (outside the guard, its docs and negative tests) or a live credential assignment | `test_repo_contains_no_live_broker_url_or_credential` |
| Intents/fills cannot be persisted as real | DB `CHECK (simulated = 1)`, `CHECK (submitted_to_broker = 0)` | `test_database_refuses_non_simulated_or_submitted_rows` |
| Paper intent creation makes no network call | Intent building and fill simulation are pure functions; tests patch sockets/HTTP to fail | `test_creating_intent_never_touches_network`, `test_no_outbound_network_during_fixture_mode` |
| Webhook notifications cannot target brokers | `WebhookChannel` runs the same host check; channels are disabled by default | `test_webhook_channel_refuses_broker_hosts` |
| Everything is labelled | `PAPER ONLY`, `SIMULATED`, `BACKTEST`, `HYPOTHETICAL SIGNAL`, `NOT SUBMITTED TO ANY BROKER` on alerts, intents, fills, positions, Top Lists, reports and in the UI header/footer | API/e2e tests |

## What "paper" means here

* **Alerts** are hypothetical signals.
* **Paper intents** are proposals with modelled spread/slippage/loss; they are stored, never transmitted.
* **Simulated fills** come from a deterministic model (see below), not from any paper broker. Real paper brokers (Alpaca paper,
  IBKR paper, TradeStation SIM) have their own simulators with different assumptions (for example instant fills or ignoring
  queue position); this project deliberately does not connect to them.
* **Backtests** are historical simulations on 1-minute bars.

## Fill simulator limitations

Deterministic and simple by design: top-of-book size or a fraction (default 10 percent) of the next bar's volume limits
the fill; partial fills are cancelled, not queued; no queue position, market impact, latency, halts, auctions, short-borrow
or fees beyond configured assumptions; stale data (over 15 s by default) or thin liquidity rejects the order. Do not treat
simulated P&L as evidence of live execution quality.

## If you ever want live trading

That is a separate, user-controlled project phase that must be designed, reviewed and authorised independently. Nothing in
this repository prepares a live path, and adding one would require removing the guard and its tests deliberately.

## Reporting a guard false positive

The guard is intentionally strict. If it rejects an environment variable that is not actually live (for example a variable
named `DELIVERY_LIVE_STATUS`), rename the variable. Do not weaken the guard.
