# Trade Ideas-Style Trading Tools: Technical Resource Map

Prepared: 2026-09-30

This map turns the earlier technical findings into implementation resources for building independent tools for scanning, real-time alerts, entry simulation, and OddsMaker-style backtesting.

## Important scope boundary

The resources below are for research and engineering. They do not authorize or perform brokerage transactions. Build and validate with paper-only adapters first. Treat live execution as a separate, user-controlled phase.

## 1. Scanning, filters, Top Lists, and market-data fields

### What to reproduce

Trade Ideas distinguishes **event-driven Alert Windows** from **snapshot-oriented Top Lists**. A practical implementation should keep those as separate pipelines:

- Alert/event detector: emits an event when a condition becomes true.
- Top List: periodically evaluates the current universe, ranks qualifying symbols, and returns a snapshot.
- Filter engine: evaluates typed predicates over quote, trade, bar, session, and derived-feature data.
- Formula engine: evaluates a constrained, versioned expression language rather than arbitrary code.

Useful implementation details from the official documentation include a 30-second Top List refresh, default Top List size of 100 and support for larger lists, AND semantics for filters, OR semantics for multiple alerts, server-side ranking, and a distinction between server sort and display sort.

### Official resources

- [Top List Window](https://www.trade-ideas.com/guide/chapter/10/10Top_List_Window.html) — refresh behavior, top-N records, filter-only semantics, and the distinction from alerts.
- [Sort Tab](https://www.trade-ideas.com/guide/chapter/10_2_7/10.2.7Sort_Tab.html) — server ranking, ascending/descending sort, secondary display sort, and record counts.
- [Layouts & Scans](https://www.trade-ideas.com/guide/chapter/29/29Layouts_and_Scans.html) — practical scan recipes and layout examples.
- [Filter Codes](https://www.trade-ideas.com/AccountManagement/FilterCodes.html) — field names, units, and formula codes across quotes, volume, price changes, ranges, fundamentals, sessions, and alert counts.
- [Formula Editor](https://www.trade-ideas.com/guide/chapter/8_3_3_1/8.3.3.1Formula_Editor.html) — reference for a bounded formula language with operators, Boolean expressions, if-statements, and account-specific formulas.
- [Price Filter](https://www.trade-ideas.com/help/filter/Price/) — example of Level-1 price semantics, min/max bounds, and Top-List eligibility.
- [Post Market Volume](https://www.trade-ideas.com/help/filter/PMVol/) — example of explicit session boundaries and today's-only extended-hours volume.

### External data-layer resources

- [Alpaca real-time stock pricing data](https://docs.alpaca.markets/docs/real-time-stock-pricing-data) — WebSocket schemas for trades, quotes, minute/daily bars, late updates, corrections, and cancel/error events.
- [IBKR historical bars](https://interactivebrokers.github.io/tws-api/historical_bars.html) — historical bar parameters, regular-trading-hours selection, bar sizes, unfinished-bar updates, adjustment behavior, and volume caveats.
- [Alpaca paper trading](https://docs.alpaca.markets/docs/paper-trading) — paper-environment behavior and limitations that affect validation.

### Build checklist

- Store filter metadata: code, type, unit, comparator, input fields, session basis, freshness, null policy, and Top-List eligibility.
- Version every formula and persist the formula version with each result.
- Define lookback limits, divide-by-zero behavior, null handling, and intrabar timing.
- Maintain quote/NBBO state, session accumulators, rolling windows, bar corrections, and reconnect recovery.
- Define corporate-action, split-adjustment, halt, delisting, and universe policies before backtests.
- Do not assume a vendor field is equivalent to a Trade Ideas field without a comparison test.

### Known gaps

Trade Ideas publishes semantic filter guidance but not a complete public scanner API, transport schema, latency guarantee, event-ordering specification, deduplication rule, or universe-history specification.

## 2. Real-time alerts, price alerts, and notifications

### What to reproduce

Model an alert as a durable state machine rather than a UI-only notification:

```text
working -> triggered
working -> invalidated
working -> expired
```

An alert event should include a stable event ID, symbol, strategy/filter ID, trigger timestamp, source timestamp, price/quote context, session, data revision, and delivery status. Separate detection from notification delivery so a missed sound or disconnected client does not erase the underlying signal.

### Official resources

- [Alert Window](https://www.trade-ideas.com/guide/chapter/9/9Alert_Window.html) — real-time event rows, customizable columns, sound alerts, and multiple simultaneous windows.
- [Create New Price Alert](https://www.trade-ideas.com/guide/chapter/15_2/15.2Create_New_Price_Alert.html) — target price, direction, expiration, invalidation price, notes, after-hours policy, and lifecycle states.
- [Alerts Dashboard](https://www.trade-ideas.com/guide/chapter/34_6/34.6Alerts.html) — high/low event dashboards, multi-strategy composition, visual distinction, and chart drill-through.
- [Audible Trade Notifications](https://www.trade-ideas.com/hollyguide/Trade_Notifications.html) — row flashing, built-in sounds, text-to-speech, volume controls, and local WAV files.
- [Stock alerts: real-time vs delayed](https://www.trade-ideas.com/resources/stock-alerts-explained-real-time-vs-delayed-alerts-in-trading/) — latency, false-positive control, signal context, and notification prioritization.

### External streaming resources

- [Alpaca WebSocket streaming market data](https://docs.alpaca.markets/us/docs/streaming-market-data) — authentication, subscriptions, wildcard symbols, connection limits, and stream lifecycle.
- [Alpaca real-time stock data](https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data) — trades, quotes, bars, timestamps, corrections, and feed differences.
- [Alpaca create an order](https://docs.alpaca.markets/us/reference/postorder) — use only as a paper-only adapter reference; keep live endpoints and credentials blocked in development.

### Build checklist

- Use trades for last-sale thresholds, quotes for bid/ask conditions, and bars for slower indicators.
- Normalize timestamps and retain both source-event time and local detection/delivery time.
- Add reconnect, resubscribe, backfill, sequence checks, stale-data detection, and slow-client handling.
- Add deduplication and cooldown keys such as `symbol + strategy_version + trigger_bucket`.
- Add quiet hours, priority routing, retry policies, notification audit logs, and client acknowledgement.
- Replay the same detector code in historical tests to reduce live/backtest drift.

### Known gaps

The reviewed Trade Ideas pages do not publish a public alert-stream API, SDK, webhook schema, authentication model, rate limits, or programmatic price-alert CRUD interface.

## 3. Holly AI signals and signal interpretation

### What to reproduce

Treat Holly as a documented product behavior, not an exposed model specification. The public materials support a conceptual architecture:

1. Generate or maintain candidate strategies.
2. Evaluate them on recent historical data.
3. Apply relevance, liquidity, news, and correlation controls.
4. Select strategies for the next session.
5. Stream intraday entries and exits.
6. Evaluate results after the session.

Use immutable, versioned signal events. A signal should preserve the strategy ID/version, symbol, direction, trigger time, entry, stop, target, hold time, stop type, source data, and lifecycle state.

### Official resources

- [How Holly AI Generates Trade Signals](https://www.trade-ideas.com/learning-center/ai-in-trading/how-holly-ai-generates-trade-signals/) — pipeline, evaluation, out-of-sample risk, regime shifts, liquidity/news gates, and correlation limits.
- [Holly Windows overview](https://www.trade-ideas.com/hollyguide/Holly_Windows.html) — separates pre-session strategy selection from intraday strategy trades.
- [AI Holly Strategy Window](https://www.trade-ideas.com/hollyguide/AI_Holly_Strategy_Window.html) — overnight testing, optimization, and next-day strategy selection.
- [AI/Holly Strategy Trades Window](https://www.trade-ideas.com/hollyguide/AI_Holly_Strategy_Trades_Window.html) — intraday trade-event behavior and new-entry flashing.
- [Show AI Trades](https://www.trade-ideas.com/guide/chapter/14_9_7/14.9.7Show_AI_Trades.html) — chart semantics for entries, stops, targets, shaded trade periods, and P&L regions.
- [Holly History](https://www.trade-ideas.com/hollyguide/Holly_History.html) — historical views, periods, profit ranking, sorting, and spreadsheet export.
- [AI Long Term Strategy Trades](https://www.trade-ideas.com/hollyguide/AI_Long_Term_Strategy_Trades_Window.html) — long-term signal fields, direction, signal price/time, returns, and profitable-day counts.
- [Holly AI virtual trade assistant](https://www.trade-ideas.com/ti-ai-virtual-trade-assistant/) — public examples of strategy conditions and product-level AI signal claims.

### Build checklist

- Separate nightly strategy promotion from intraday signal streaming and post-session evaluation.
- Preserve exact strategy versions and feature dependencies.
- Run walk-forward and out-of-sample validation; do not treat nightly optimization as proof of robustness.
- Add point-in-time universes, corporate actions, halts, delistings, costs, and latency sensitivity.
- Make every chart overlay traceable to raw event data and calculations.
- Log skipped signals, invalidated signals, and signals rejected by risk or liquidity gates.

### Known gaps

Trade Ideas does not publish Holly model code, complete features, selector/scoring formulas, full parameter sets, a public signal API, a webhook contract, or an official machine-readable signal schema. Do not treat UI scraping or External Linking as a stable Holly API.

## 4. OddsMaker-style event-driven backtesting

### What to reproduce

Implement the backtester as an event replay engine, not merely a bar-by-bar indicator tester:

- Ingest an immutable event stream.
- Sort events chronologically.
- Apply entry eligibility and per-symbol/day rules.
- Resolve exits using a visible, deterministic fill policy.
- Produce per-trade, daily, equity, drawdown, and filter-attribution outputs.

The current official guide documents 1-minute OHLC-based history, chronological alert entry, no current-day/premarket/postmarket testing, one entry per symbol per day, and a daily trade cap. Public materials differ on exact history depth, so make the actual date range explicit in every run.

### Official resources

- [Backtesting/Oddsmaker](https://www.trade-ideas.com/guide/chapter/22/22Backtesting_Oddsmaker.html) — primary current mechanics, metrics, history/cap notes, OHLC limitations, and paper-testing recommendation.
- [OddsMaker User's Guide v2](https://www.trade-ideas.com/OddsMaker/Help.version2.html) — detailed entry units/directions, time windows, five-minute exit rounding, multi-day exits, stop/target/Wiggle behavior, metrics, and run rules.
- [Entry Tab](https://www.trade-ideas.com/guide/chapter/22_1/22.1Entry_Tab.html) — regular-session entry windows and session exclusions.
- [Timed Exit Tab](https://www.trade-ideas.com/guide/chapter/22_2/22.2Timed_Exit_Tab.html) — timed, time-of-day, and multi-day exits.
- [Risk Management Tab](https://www.trade-ideas.com/guide/chapter/22_3/22.3Risk_Management_Tab.html) — dollar/percent/filter targets and stops, Wiggle behavior, and live-order limitations.
- [Advanced Exit Tab](https://www.trade-ideas.com/guide/chapter/22_4/22.4Advanced_Exit_Tab.html) — trailing stops, alert exits, and optimization dimensions.
- [OddsMaker Backtesting Guide](https://www.trade-ideas.com/learning-center/backtesting-strategy-development/oddsmaker-backtesting-guide/) — workflow for defining entries, exits, universes, date ranges, costs, and paper validation.
- [How to Evaluate a Trading Strategy](https://www.trade-ideas.com/learning-center/backtesting-strategy-development/how-to-evaluate-a-trading-strategy/) — sensitivity, slippage/cost stress, universes, regimes, walk-forward tests, and live-sample framing.

### External data and calendar resources

- [Massive custom minute bars](https://massive.com/docs/rest/stocks/aggregates/custom-bars) — minute OHLCV, ET timestamps, adjusted/unadjusted data, empty intervals, pagination, and data-history considerations.
- [NYSE trading hours and calendars](https://www.nyse.com/markets/hours-calendars) — regular hours, early closes, holidays, and session definitions.
- [Alpaca paper trading](https://docs.alpaca.markets/us/docs/paper-trading) — forward-validation simulator limitations.

### Build checklist

- Snapshot every run input: alert/filter configuration, universe, direction, session, date range, exits, costs, slippage, equity, and position sizing.
- Define the intrabar policy when both stop and target fall inside one OHLC bar.
- Define gap-through behavior, missing bars, halts, auctions, split adjustment, dividends, short borrow, and delistings.
- Keep success classification separate from net P&L.
- Report trade count, win rate, expectancy, profit factor, average win/loss, drawdown, buying-power peak, daily P&L, holding time, and exit reason.
- Add parameter sensitivity, walk-forward, regime splits, Monte Carlo reshuffling, and out-of-sample tests.
- Label all modeled fills, returns, fees, and balances as hypothetical.

### Known gaps

Trade Ideas does not publish a full OddsMaker computation/API specification. Public materials do not fully specify data corrections, exact indicator availability, stop/target collision ordering, slippage, spread, partial fills, survivorship, corporate actions, or all formulas behind projected return and statistical-confidence measures.

## 5. Paper entries, Brokerage Plus concepts, and risk controls

### What to reproduce

Keep the execution architecture separate from the signal engine:

```text
Signal event -> risk gate -> paper order intent -> adapter -> simulated acknowledgement/fill -> reconciliation
```

Build a broker-neutral order model, then map only supported order types per adapter. Keep paper and live environments isolated at the configuration and runtime layers. A paper environment should fail closed if a live endpoint or credential is supplied.

### Official resources

- [Custom One-Click Order Entry Template](https://www.trade-ideas.com/guide/chapter/21_4_2_1/21.4.2.1Create_a_OneClick_Order_Entry_Template_.html) — sizing by shares/dollars/stop risk, order types, time-in-force, offsets, stops, targets, timed exits, and scan/chart invocation.
- [Create an Auto-Trading Strategy](https://www.trade-ideas.com/guide/chapter/21_4_2_2/21.4.2.2Create_an_AutoTrading_Strategy_.html) — scan-to-automation lifecycle, simulator-first workflow, daily enablement, account assignment, and documented trade cap.
- [Stop Limit and Stop Market Orders](https://www.trade-ideas.com/guide/chapter/21_4_2_3/21.4.2.3How_to_use_Stop_Limit_and_Stop_Market_Orders_as_Entry_Orders_.html) — static formula requirements and immediate-routing behavior.
- [Connect to Interactive Brokers](https://www.trade-ideas.com/guide/chapter/21_1_2/21.1.2Connect_to_Interactive_Brokers.html) — IB Pro requirement, TWS settings, live/paper ports, client ID, and market-data dependency.
- [Trade Ideas broker integrations](https://www.trade-ideas.com/partners/) — current partner and integration inventory.

### External paper-broker resources

- [Alpaca paper trading](https://docs.alpaca.markets/docs/paper-trading) — separate paper endpoint/keys and documented simulator omissions.
- [Alpaca orders](https://docs.alpaca.markets/docs/orders-at-alpaca) — bracket/OCO/OTO, stop-limit, trailing stop, activation, replacement, and extended-hours constraints.
- [IBKR paper trading account](https://www.interactivebrokers.com/campus/glossary-terms/paper-trading-account/) — paper API availability and simulator-specific limitations.
- [TradeStation SIM vs LIVE](https://api.tradestation.com/docs/fundamentals/sim-vs-live/) — separate simulated base URL and instant-fill warning.

### Build checklist

- Enforce max risk per trade, max concurrent positions, daily loss lockout, max notional, exposure caps, symbol whitelist, and a kill switch.
- Record every signal, risk decision, order intent, acknowledgement, fill, rejection, cancellation, and reconciliation result.
- Use idempotency keys and handle duplicate events, disconnects, retries, partial fills, and stale market data.
- Map broker-native order capabilities explicitly; reject unsupported combinations before submission.
- Compare deterministic backtest fills with paper fills and report drift by symbol, session, spread, and latency.

### Known gaps

Trade Ideas documents Brokerage Plus as a product workflow, not a general public execution API. The reviewed pages do not provide a public Trade Ideas webhook, event schema, SDK, rate-limit specification, or stable external order-routing contract.

## 6. External linking, broker APIs, and data architecture

### Trade Ideas-specific integration

- [External Linking](https://www.trade-ideas.com/guide/chapter/8_3_2/8.3.2External_Linking.html) — desktop symbol transfer into a focused chart/order-entry field, regex translation, multi-platform broadcast, Idea Surfing, focus requirements, startup order, and persistence behavior.
- [Connecting to Brokerage Plus](https://www.trade-ideas.com/education/connecting-to-brokerage-plus/) — product connection workflow and supported broker setup concepts.
- [Brokers](https://www.trade-ideas.com/brokers/) — official inventory distinguishing API Integration pages from partner-access pages.
- [Features](https://www.trade-ideas.com/features/) — product map for streaming alerts, Top Lists, paper trading, OddMaker, Brokerage Plus, and External Linking.

External Linking should be treated as an optional Windows desktop adapter, not as the core integration. It depends on focus, local UI state, and keystroke-style symbol transfer.

### Independent broker/API resources

- [IBKR Web API](https://www.interactivebrokers.com/campus/ibkr-api-page/web-api-trading/) — account, portfolio, market-data, contract, and trading services.
- [IBKR paper trading limitations](https://www.interactivebrokers.com/campus/glossary-terms/paper-trading-account/) — simulator-specific order and fill behavior.
- [TradeStation API documentation](https://api.tradestation.com/docs/) — public brokerage and market-data API overview.
- [TradeStation API specification](https://api.tradestation.com/docs/specification/) — bars, streams, orders, positions, authentication, and simulated-environment details.
- [Alpaca paper trading](https://docs.alpaca.markets/us/docs/paper-trading) — clean paper/live endpoint separation and simulation caveats.
- [Alpaca WebSocket market data](https://docs.alpaca.markets/docs/streaming-market-data) — real-time ingestion protocol.
- [Massive API documentation](https://massive.com/docs/) — separation of historical REST, real-time WebSockets, and bulk historical files.

### Build checklist

- Define a normalized internal contract for bars, quotes, trades, signals, order intents, order states, and fills.
- Persist raw vendor events and normalized events so scans can be replayed.
- Add feed entitlement checks, rate-limit handling, reconnect/backfill, heartbeat monitoring, and clock synchronization.
- Encrypt and scope credentials; never place secrets in logs.
- Enforce vendor licensing, retention, redistribution, and exchange-attribution requirements.
- Start with one paper broker adapter and one market-data provider before adding multiple providers.

### Key conclusion

The reviewed official Trade Ideas pages do not expose a public developer API for extracting scanner events, Holly signals, or OddsMaker results. The safest architecture is therefore:

1. Build the market-data, scanner, alert, backtest, and paper-execution layers independently.
2. Use Trade Ideas documentation as a behavioral and UX benchmark.
3. Use official broker/data APIs for machine-readable development interfaces.
4. Treat External Linking as an optional manual handoff, not an automation backbone.
5. Ask Trade Ideas directly about private partner/API access before designing around an undocumented interface.

## Recommended build order

### Stage A — deterministic scanner core

Implement the typed filter and formula engine, event detector, Top List snapshotter, session calendar, raw-data store, and replay tests.

### Stage B — real-time alert service

Add WebSocket ingestion, alert state machines, deduplication, reconnect/backfill, notification routing, and an alert audit log.

### Stage C — OddsMaker-compatible research backtester

Add chronological event replay, 1-minute OHLC rules, visible fill assumptions, risk/exit configuration, daily/equity metrics, and walk-forward evaluation.

### Stage D — paper-only forward validation

Connect one paper market-data/broker adapter. Compare live alerts, simulated fills, and backtest expectations. Do not interpret paper P&L as proof of live execution quality.

### Stage E — integration hardening

Add broker capability mapping, reconciliation, kill switches, stale-feed detection, security controls, data licensing controls, and reproducibility reports.

## Reference caveats to preserve in product documentation

- OddsMaker is documented as 1-minute OHLC-based, not tick-by-tick.
- Trade Ideas materials vary on historical depth; always display the actual test range.
- Historical exits do not necessarily model spread, latency, queue position, partial fills, or market impact.
- Paper brokers have materially different simulator assumptions.
- UI External Linking is not equivalent to a public API.
- No source reviewed guarantees future performance or live execution quality.
