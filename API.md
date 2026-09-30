# API

Base URL `http://127.0.0.1:8000`. Interactive OpenAPI docs are generated at `/docs` (Swagger UI) and `/openapi.json`.
**There is no order endpoint.** Everything that looks like trading (`/api/paper-*`) is a simulation and is labelled
`simulated: true` / `submitted_to_broker: false`.

## Endpoint index

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Health: data-provider status, market session, last event time, paper-only mode |
| GET | `/api/status` | Full market/feed status (same data the status WebSocket pushes) |
| GET | `/api/config/public` | Redacted configuration + safety facts (`live_trading_supported: false`) |
| GET | `/api/features` | Feature registry (names, units, types, lookbacks) for building filters |
| GET | `/api/symbols` | Symbols and activation state |
| POST | `/api/symbols/{symbol}/active` | Activate/deactivate a symbol (e.g. delisted) |
| GET | `/api/strategies` | Current version of every strategy |
| POST | `/api/strategies` | Create a strategy (version 1). 422 on validation errors, 409 if it exists |
| GET | `/api/strategies/{id}` | One strategy (`?version=N` for history) |
| GET | `/api/strategies/{id}/versions` | Version history with config hashes |
| PUT | `/api/strategies/{id}` | Save changes as a **new immutable version** |
| POST | `/api/strategies/{id}/validate` | Validate a draft body (or the stored strategy) without saving |
| POST | `/api/strategies/{id}/preview` | Evaluate a draft/stored strategy against current data. Creates no alerts |
| GET | `/api/alerts` | Alert history (`status`, `symbol`, `strategy_id`, `limit`, `offset`) |
| GET | `/api/alerts/{event_id}` | Alert + deliveries + paper intents |
| POST | `/api/alerts/{event_id}/acknowledge` | `triggered` to `acknowledged` (409 for illegal transitions) |
| GET | `/api/alerts/{event_id}/chart` | Stored 1-minute bars around the alert |
| GET | `/api/top-lists/{strategy_id}` | Ranked snapshot (`refresh=false` returns the cached one, `max_rows`) |
| POST | `/api/backtests` | Run a backtest (202; `wait: true` blocks until done) |
| GET | `/api/backtests` | Recent runs |
| GET | `/api/backtests/{run_id}` | Run with full report and preserved strategy snapshot |
| GET | `/api/backtests/{run_id}/trades` | Simulated trades (`limit`, `offset`) |
| POST | `/api/paper-intents` | Create a **simulated** intent (+ optional simulated fill). Never calls a broker |
| GET | `/api/paper-intents`, `/api/paper-fills` | Persisted intents / fills |
| GET | `/api/paper-positions` | Simulated positions (in memory for the session) |
| POST | `/api/notifications/test` | Test notification (non-transactional) |
| GET/PUT | `/api/preferences` | Notification preferences and quiet hours |
| GET | `/api/deliveries`, `/api/audit` | Notification delivery audit, audit log |
| POST | `/api/dev/replay/run`, `/api/dev/replay/restart` | Fixture provider only: control the replay |
| WS | `/ws/alerts` | Alert stream |
| WS | `/ws/market-status` | Status stream |

Errors: `404` unknown id, `409` conflict / illegal state transition, `422` validation. Paper-intent rejections return
`{"detail": {"error": "intent_rejected", "detail": "...", "broker_submission": false}}`.

## Health

`GET /health`

```json
{
  "status": "ok",
  "version": "0.1.0",
  "paper_only": true,
  "trading_mode": "paper",
  "data_provider": {
    "provider": "fixture",
    "connected": true,
    "feed": "synthetic",
    "last_event_ts": null,
    "last_ingest_ts": null,
    "reconnects": 0,
    "dropped_events": 0,
    "duplicate_events": 0,
    "rate_limited": 0,
    "gaps": 0,
    "late_events": 0,
    "corrections": 0,
    "stale": false,
    "detail": ""
  },
  "feed_state": "replaying",
  "market_session": "regular",
  "last_event_time": null,
  "labels": [
    "PAPER ONLY",
    "SIMULATED"
  ]
}
```

## Strategy (create / update / validate / preview)

Request body for `POST /api/strategies`, `PUT /api/strategies/{id}`, `POST .../validate`, `POST .../preview`:

```json
{
  "id": "my-breakout",
  "name": "My breakout",
  "direction": "long",
  "universe": ["ORBX", "GAPU"],
  "filters": [
    {"id": "min-price", "name": "Price >= 5", "field": "last", "operator": "gte", "value": 5, "unit": "usd"},
    {"id": "liquid", "name": "Dollar volume >= 500k", "field": "dollar_volume", "operator": "gte", "value": 500000, "null_policy": "fail"}
  ],
  "alert_conditions": [
    {
      "id": "vwap-cross", "name": "Above VWAP with volume", "event_type": "vwap_break", "priority": "high",
      "cooldown_seconds": 900, "dedupe_bucket_seconds": 300, "confirm_bars": 0, "expires_after_seconds": 3600,
      "filters": [
        {"id": "rvol", "name": "RVOL >= 1.5", "field": "rvol", "operator": "gte", "value": 1.5},
        {"id": "orb", "name": "ORB breakout", "field": "orb_breakout_up", "operator": "is_true", "lookback": 15},
        {"id": "f", "name": "custom", "field": "formula", "operator": "is_true",
         "formula": "vwap_dist_pct > 0.3 and ret(5) > 0.2 and spread_bps < 40"}
      ]
    }
  ],
  "ranking": {"field": "rvol", "order": "desc"},
  "display_sort": {"field": "pct_change", "order": "desc"},
  "top_list": {"refresh_seconds": 30, "max_rows": 100}
}
```

* Strategy `filters` are **AND**-combined and gate everything (alerts and Top List). `alert_conditions` are **OR**-combined
  triggers; the filters inside one condition are AND-combined.
* Filter fields: any name from `GET /api/features`, or `"field": "formula"` with a `formula` (see below).
* Operators: `gt gte lt lte eq neq between outside is_true is_false`. `between`/`outside` take `[low, high]`.
* `unit` is optional; if given it must equal the field's unit. `lookback` is only valid for features that take one and is
  capped at 400 bars (opening range: minutes, max 120).
* `null_policy`: `fail` (default), `pass`, or `skip` (filter ignored when its value is null).
* `session_basis`: `regular` (default), `extended`, `premarket`, `postmarket`.

Validation response (`POST /api/strategies/x/validate`), invalid case:

```json
{
  "valid": false,
  "errors": [
    {
      "path": "filters.0",
      "message": "unit mismatch: field 'last' is in 'usd', filter declares 'pct'"
    }
  ],
  "warnings": []
}
```

Valid case: `{"valid": true, "errors": [], "warnings": [...], "spec": {...normalised strategy...}}`.

### Formula language

`rvol > 2 and vwap_dist_pct > 0.5`, `if(spread_bps > 0, last / vwap, 1) > 1`, `sma(20) < last`, `coalesce(rvol, 0) >= 1`.
Operators `+ - * /`, comparisons, `and or not` (`&& || !`), functions `abs min max coalesce isnull if`, features with an
integer-literal lookback `sma(20)`. Unit-checked (`last + pct_change` is rejected), null-propagating (Kleene logic for
`and`/`or`), division by zero yields null. No code execution is possible: unknown identifiers, calls, attribute access,
strings and statements are all validation errors reporting the column. Limits: 500 characters, 120 nodes, depth 24.

## Alert event

Returned by `GET /api/alerts`, the WebSocket and persisted in `alert_events`. `source_timestamp` is provider time,
`detected_timestamp` is local detection time and `delivered_timestamp` is local delivery time.

```json
{
  "event_id": "evt_8cc96047ec908ddf4c1d",
  "symbol": "GAPU",
  "strategy_id": "opening-range-breakout",
  "strategy_version": 1,
  "condition_id": "orb-up",
  "direction": "long",
  "event_type": "breakout",
  "source_timestamp": "2026-09-28T13:52:00.000Z",
  "detected_timestamp": "2026-09-28T13:52:00.000Z",
  "session": "regular",
  "trigger_price": 48.87,
  "bid": 48.86,
  "ask": 48.88,
  "spread_bps": 4.092,
  "feature_snapshot": {
    "last": 48.87,
    "bid": 48.86,
    "ask": 48.88,
    "spread_bps": 4.09249,
    "rvol": 1.793135,
    "vwap": 48.763632,
    "vwap_dist_pct": 0.218131,
    "orb_high": 48.86,
    "orb_low": 48.64,
    "pct_change": 6.936543,
    "volume": 2661723.0
  },
  "filter_snapshot": {
    "strategy_id": "opening-range-breakout",
    "strategy_version": 1,
    "condition_id": "orb-up",
    "strategy_filters": [
      {
        "filter_id": "min-price",
        "filter_version": 1,
        "field": "last",
        "operator": "gte",
        "threshold": 2.0,
        "observed": 48.87,
        "passed": true,
        "null": false,
        "skipped": false,
        "notes": []
      }
    ],
    "condition_filters": [
      {
        "filter_id": "orb-up",
        "filter_version": 1,
        "field": "orb_breakout_up",
        "operator": "is_true",
        "threshold": null,
        "observed": true,
        "passed": true,
        "null": false,
        "skipped": false,
        "notes": []
      }
    ],
    "config": "<full strategy configuration snapshot, includes config_hash and formula digests>"
  },
  "status": "triggered",
  "status_reason": "",
  "priority": "high",
  "dedupe_key": "GAPU|opening-range-breakout|v1|orb-up|1989559",
  "expires_at": "2026-09-28T14:52:00.000Z",
  "confirm_at": null,
  "acknowledged_at": null,
  "acknowledged_by": null,
  "delivered_timestamp": "2026-09-28T13:52:00.000Z",
  "delivery_delay_ms": 0,
  "stale_data": false,
  "delayed_delivery": false,
  "data_provider": "fixture",
  "data_feed": "synthetic",
  "strategy_config_hash": "c6cd34d2e7dd4b84",
  "paper_only": true,
  "hypothetical": true,
  "label": "HYPOTHETICAL SIGNAL - PAPER ONLY - NOT AN ORDER"
}
```

States: `working`, `triggered`, `invalidated`, `expired`, `acknowledged`, `suppressed`. Legal transitions:
`working` to `triggered|invalidated|expired|suppressed`; `triggered` to `acknowledged|invalidated|expired`; the rest are
terminal. `suppressed` rows (cooldown, duplicate dedupe key, daily cap) are kept with `status_reason` for audit.

## Top List snapshot

`GET /api/top-lists/{strategy_id}` (a separate pipeline from event alerts; only the first row is shown here)

```json
{
  "strategy_id": "opening-range-breakout",
  "strategy_version": 1,
  "config_hash": "c6cd34d2e7dd4b84",
  "as_of": "2026-09-28T14:00:01.000Z",
  "generated_at": "2026-09-28T14:00:01.000Z",
  "evaluated": 8,
  "qualified": 2,
  "returned": 2,
  "rows": [
    {
      "rank": 2,
      "symbol": "GAPU",
      "score": 1.802515,
      "last": 48.94,
      "pct_change": 7.0897,
      "rvol": 1.803,
      "volume": 3362089.0,
      "spread_bps": 4.086636697998188,
      "features": {
        "vwap_dist_pct": 0.2986,
        "gap_pct": 6.8053,
        "range_pct": 0.7152,
        "dollar_volume": 164052957.15
      }
    }
  ],
  "data_age_seconds": 1.0,
  "stale": false,
  "ranking": {
    "field": "rvol",
    "formula": null,
    "order": "desc"
  },
  "display_sort": {
    "field": "pct_change",
    "order": "desc"
  },
  "paper_only": true,
  "label": "HYPOTHETICAL SCAN RESULT - NOT AN ORDER",
  "refresh_seconds": 30.0
}
```

## Paper intent (simulated)

`POST /api/paper-intents`. Provide `event_id` (from a `triggered`/`acknowledged` alert) or `symbol`+`direction`+`reference_price`;
and exactly one of `quantity` or `risk_dollars`. `entry_model`: `next_trade`, `next_bar_open`, `bid_ask_cross`,
`fixed_slippage`, `spread_plus_slippage`. `stop`: `percent|dollars|atr|none`; `target`: `percent|dollars|r_multiple|none`.
`next_trade`/`next_bar_open` intents stay `pending` until the next print/bar; the others fill immediately from current data.

```json
{
  "intent": {
    "intent_id": "pi_8814221f9a614be9",
    "event_id": "evt_8cc96047ec908ddf4c1d",
    "symbol": "GAPU",
    "direction": "long",
    "quantity": 189.0,
    "entry_model": "spread_plus_slippage",
    "stop_model": {
      "type": "percent",
      "value": 1.0
    },
    "target_model": {
      "type": "r_multiple",
      "value": 2.0
    },
    "time_exit_minutes": 60,
    "est_spread_bps": 4.092,
    "est_slippage_bps": 2.0,
    "max_modeled_loss": 99.88,
    "strategy_id": "opening-range-breakout",
    "strategy_version": 1,
    "status": "filled",
    "created_at": "2026-09-28T14:00:01.000Z",
    "expires_at": "2026-09-28T14:15:01.000Z",
    "reference_price": 48.87,
    "est_entry_price": 48.8898,
    "stop_price": 48.4009,
    "target_price": 49.8676,
    "detail": {
      "spread_assumed": false,
      "max_modeled_loss_formula": "qty * (stop_distance + 2*slippage + spread)",
      "per_share_risk": 0.528443
    },
    "simulated": true,
    "submitted_to_broker": false,
    "label": "SIMULATED PAPER INTENT - NOT SUBMITTED TO ANY BROKER",
    "assumptions": "Hypothetical; spread/slippage are modelled assumptions, not observed executions."
  },
  "fills": [
    {
      "fill_id": "pf_9ac788e33e484db2",
      "intent_id": "pi_8814221f9a614be9",
      "symbol": "GAPU",
      "side": "buy",
      "quantity": 189.0,
      "price": 48.9598,
      "fill_ts": "2026-09-28T14:00:01.000Z",
      "model": "spread_plus_slippage",
      "partial": false,
      "status": "filled",
      "reject_reason": null,
      "detail": {
        "requested": 189.0,
        "available": 9982,
        "unfilled_cancelled": 0.0,
        "note": "SIMULATED - never sent to a broker"
      }
    }
  ],
  "broker_submission": false,
  "simulated": true,
  "label": "SIMULATED PAPER INTENT - NOT SUBMITTED TO ANY BROKER"
}
```

Fill statuses: `filled`, `partial`, `rejected` (`stale data` or `insufficient liquidity`). `max_modeled_loss = qty * (stop distance
+ 2*slippage + spread)`.

## Backtest

`POST /api/backtests` returns `202 {"run_id", "status", "label"}`.

```json
{
  "strategy_id": "opening-range-breakout",
  "strategy_version": null,
  "wait": true,
  "config": {
    "start_date": "2026-09-08", "end_date": "2026-09-25",
    "symbols": ["ORBX", "GAPU"],
    "universe_mode": "static",
    "direction": "strategy",
    "entry_price_model": "next_open",
    "entry_start": "09:35", "entry_end": "15:30",
    "one_entry_per_symbol_per_day": true,
    "max_trades_per_day": 20, "max_concurrent_positions": 5, "daily_loss_limit": 500,
    "starting_equity": 100000, "leverage": 1.0,
    "sizing": {"mode": "risk_percent", "risk_percent": 0.5},
    "exits": {
      "profit_target": {"type": "percent", "value": 1.5},
      "stop_loss": {"type": "percent", "value": 0.75},
      "trailing_stop": {"type": "percent", "value": 1.0},
      "time_after_entry_minutes": 90, "time_of_day_exit": "15:45",
      "hold_days": 0, "multi_day_exit": "close",
      "exit_filters": []
    },
    "costs": {"commission_per_share": 0.005, "min_commission_per_order": 1.0, "fee_bps": 0.2, "spread_bps": 5, "slippage_bps": 2},
    "intrabar_policy": "stop_first",
    "assumed_spread_bps": 6
  },
  "universe": null
}
```

`point_in_time` mode requires `"universe": {"snapshots": {"2026-09-01": ["AAA","BBB"]}, "delisted": {"BBB": "2026-09-20"}}`.
Extended-hours backtests are rejected (regular session only).

Report (`GET /api/backtests/{run_id}`, field `report`; long series abbreviated):

```json
{
  "label": "HISTORICAL SIMULATION on 1-minute OHLC bars. Not a prediction or guarantee of future results. All fills, costs and balances are hypothetical.",
  "strategy": {
    "config_hash": "c6cd34d2e7dd4b84",
    "id": "opening-range-breakout",
    "name": "Opening Range Breakout (15m)",
    "version": 1
  },
  "data": {
    "provider": "fixture",
    "feed": "synthetic",
    "adjustment": "as-provided",
    "bar_size": "1Min",
    "requested_range": [
      "2026-09-08",
      "2026-09-25"
    ],
    "first_trading_day": "2026-09-08",
    "last_trading_day": "2026-09-25",
    "bars_used": 43680,
    "bars_expected": 43680,
    "overall_coverage_pct": 100.0
  },
  "metrics": {
    "ambiguous_bars_excluded": 0,
    "average_loser": -39.7342,
    "average_winner": 74.8182,
    "avg_trades_per_day": 0.8571,
    "buying_power_peak": 9982.54,
    "drawdown_basis": "closed-trade equity plus end-of-day mark-to-market",
    "ending_equity": 100783.27,
    "expectancy": 65.2722,
    "gross_pnl": 812.43,
    "losers": 1,
    "max_consecutive_losses": 1,
    "max_consecutive_wins": 9,
    "max_drawdown": 39.74,
    "max_drawdown_pct": 0.0397
  },
  "exit_reasons": {
    "profit_target": 11,
    "stop_loss": 1
  },
  "skipped_signals": {
    "alert suppressed (cooldown)": 2,
    "alert suppressed (duplicate)": 3,
    "outside entry window": 1
  },
  "intrabar_policy": "stop_first",
  "...": "equity_curve, drawdown_series, daily_pnl, trades_per_day, holding_time, attribution, assumptions, config"
}
```

A simulated trade (`GET /api/backtests/{run_id}/trades`):

```json
{
  "commission": 0.0,
  "condition_id": "orb-up",
  "costs": 2.2488,
  "direction": "long",
  "entry_price": 43.0994,
  "entry_ref_price": 43.08,
  "entry_ts": "2026-09-08T13:48:00.000Z",
  "event_id": "evt_6155aec4e2cc10bb9a57",
  "exit_price": 43.7459,
  "exit_reason": "profit_target",
  "exit_ref_price": 43.7459,
  "exit_ts": "2026-09-08T14:18:00.000Z",
  "gross_pnl": 77.2417,
  "holding_minutes": 30.0,
  "net_pnl": 74.9929,
  "quantity": 116,
  "return_pct": 1.5007,
  "simulated": true,
  "slippage_cost": 0.9995,
  "spread_cost": 1.2493,
  "symbol": "GAPU",
  "trade_no": 1
}
```

The sample numbers come from **synthetic** fixture data; they demonstrate the report shape only.

## Status message

`GET /api/status` and every `/ws/market-status` message (`type: "status"`):

```json
{
  "paper_only": true,
  "trading_mode": "paper",
  "labels": [
    "PAPER ONLY",
    "SIMULATED"
  ],
  "market_session": "postmarket",
  "is_trading_day": true,
  "early_close": false,
  "clock": "2026-09-28T20:00:00.000Z",
  "provider": {
    "provider": "fixture",
    "connected": false,
    "feed": "synthetic",
    "last_event_ts": "2026-09-28T20:00:00.000Z",
    "last_ingest_ts": "2026-09-30T03:13:28.032Z",
    "reconnects": 0,
    "dropped_events": 0,
    "duplicate_events": 0,
    "rate_limited": 0,
    "gaps": 0,
    "late_events": 0,
    "corrections": 0,
    "stale": false,
    "detail": ""
  },
  "feed_state": "replay_complete",
  "last_event_time": "2026-09-28T20:00:00.000Z",
  "symbols": 8,
  "alerts_total": 29,
  "alerts_triggered": 0,
  "events_processed": 12482,
  "strategies_enabled": 2,
  "recent_provider_events": [
    {
      "kind": "market_open",
      "symbol": null,
      "ts": "2026-09-28T13:30:00.000Z",
      "detail": "2026-09-28"
    }
  ]
}
```

Feed states: `live`, `stale` (no data for `STALE_FEED_SECONDS` during the regular session), `disconnected`, `replaying`,
`replay_complete`, `stopped`.

## WebSocket `/ws/alerts`

Server to client:

```json
{"type": "hello", "paper_only": true, "recent": [ /* last 50 alerts */ ]}
{"type": "alert", "alert": { /* AlertEvent, status triggered */ }, "delivered_at": "2026-09-28T13:51:00.000Z", "warnings": ["DATA DELAY"], "paper_only": true}
{"type": "alert_update", "alert": { /* status changed: acknowledged, expired, invalidated ... */ }, "paper_only": true}
{"type": "test_notification", "alert": { /* synthetic, never listed as an alert */ }, "warnings": [], "paper_only": true}
{"type": "top_list", "strategy_id": "opening-range-breakout", "snapshot": { /* Top List snapshot */ }}
{"type": "ack_ok", "alert": { }}   {"type": "error", "detail": "..."}   {"type": "pong", "ts": "..."}
```

`warnings` may contain `STALE FEED` (data age at detection above the threshold) and `DATA DELAY` (delivery latency above
`DELAYED_DELIVERY_MS`). Client to server: `{"type": "ack", "event_id": "..."}`, `{"type": "ping"}`.
Slow clients get a bounded queue (oldest messages are dropped, never the pipeline).

## WebSocket `/ws/market-status`

Sends one `status` message on connect and again on every provider/session event and roughly once per second.

## Provider event contracts (internal, normalised)

| Event | Fields |
| --- | --- |
| `Trade` | `symbol, ts, price, size, trade_id, conditions, ingest_ts` |
| `Quote` (NBBO) | `symbol, ts, bid, ask, bid_size, ask_size, ingest_ts` |
| `Bar` (1 minute, start-stamped) | `symbol, ts, open, high, low, close, volume, vwap, trade_count, revision, corrected, ingest_ts`; `revision > 0` / `corrected` = updated bar |
| `SessionEvent` | `kind_` (`market_open`, `market_close`, `halt`, `resume`), `ts`, `symbol`, `detail` |
| `ProviderEvent` | `kind_` (`connected`, `disconnected`, `reconnecting`, `error`, `stale`, `gap`, `rate_limited`, `backpressure`), `detail` |
