# Architecture

**Paper-only.** No component in this design can place, route, modify or cancel a real order.

## Decisions (reversible engineering choices)

| Decision | Choice | Why / how to reverse |
| --- | --- | --- |
| Backend | Python 3.11 + FastAPI, one process, asyncio tasks | Small reliable system, no microservices. |
| Transport to browser | WebSocket (`/ws/alerts`, `/ws/market-status`) + REST | Alerts must be pushed; REST for everything else. |
| Storage | SQLAlchemy Core + plain portable SQL migrations; SQLite dev default, PostgreSQL-compatible schema | Timestamps are ISO-8601 UTC `TEXT` and JSON is `TEXT` so the same SQL runs on both. Move to `timestamptz`/`jsonb` in a later migration if PostgreSQL becomes the primary target. |
| Frontend | Dependency-free SPA in `src/scanalert/ui` (no build step) | No frontend existed; works on Windows without Node. Swap for React later; the REST/WS contract is unchanged. |
| Market data | `MarketDataProvider` protocol; `FixtureProvider` (offline) and `AlpacaProvider` (data only) | One concrete dev adapter and one real data adapter, as requested. |
| Tests | pytest + pytest-asyncio | UI has no test framework (no build); it is covered by API tests, the smoke test and a headless-browser check performed during development. |

## Component map

```
                 +---------------------------+
 Provider  -->   | ResilientStream (alpaca)  |  reconnect, resubscribe, stale, backpressure,
 (fixture or     | / FixtureProvider replay  |  dedupe, gaps, rate limits, auth failure stop
  alpaca data)   +-------------+-------------+
                               | normalised events (Trade, Quote, Bar, SessionEvent, ProviderEvent)
                               v
                 +---------------------------+     raw events + bars persisted (both timestamps)
                 | Service.handle_event      |---> Store (SQLAlchemy Core, migrations)
                 +------+---------+----------+
                        |         |
              SymbolState     paper book (pending intents,
              (features)      simulated positions/fills)
                        |
        +---------------+----------------+
        v                                v
  EventScanner                     SnapshotScanner
  (transition detection)           (rank, top-N, display sort)
        |                                |
  AlertManager                     Top List API/WS
  (state machine, dedupe,
   cooldown, suppression audit)
        |
  NotificationRouter --> WebSocketChannel (browser) | WebhookChannel (off) | EmailChannel (off)
        |               retries, quiet hours, priority, delivery audit
        v
     FastAPI + WebSocket hub + static UI

 Backtester (backtest.py) re-uses EventScanner + AlertManager + SymbolState on historical 1-minute bars.
```

## Modules

| Module | Responsibility |
| --- | --- |
| `config.py` | Typed settings; the fail-closed paper guard (`assert_paper_only`, `scan_environment`, `inspect_url`). |
| `models.py` | Provider-neutral events. Every event has source `ts` and local `ingest_ts`. |
| `calendar.py` | NYSE rules: holidays incl. observed dates, Good Friday (Easter algorithm), early closes, special closures, DST-correct ET conversion, session classification. `ExchangeCalendar` protocol allows substituting an official calendar. |
| `features.py` | `SymbolState` (incremental accumulators, corrections, late bars) and the feature registry (~48 features incl. spread, volume, RVOL, change, range, VWAP distance, opening range, new high/low, MAs, volatility, time of day, liquidity). |
| `formula.py` | Tokenizer, Pratt parser, static type+unit checker, null-propagating evaluator, canonical form + digest. |
| `filters.py` / `strategy.py` | Typed versioned `FilterSpec`; `StrategySpec` = AND gate filters + OR alert conditions + ranking + Top List config. |
| `scanner.py` | Event scanner and snapshot scanner (separate pipelines). |
| `alerts.py` | `AlertEvent`, transition table, `AlertManager` (dedupe key `symbol+strategy+version+condition+time bucket`, cooldown, daily cap, expiry, confirmation window). |
| `notify.py` | Hub (bounded per-client queues), preferences, quiet hours, retries with backoff, delivery audit, delay/stale flags. |
| `paper.py` | Intent construction, fill simulation, shared stop/target collision policy. No I/O. |
| `backtest.py` | Event-replay engine and reporting. |
| `db.py` | Engine factory, migration runner (checksummed), `Store` repository (lock-serialised). |
| `service.py` | Wiring, read models, paper book, backtest jobs. |
| `api.py` | REST + WebSocket + static UI. |
| `providers/` | `base` protocol, `stream` (resilience core), `alpaca`, `fixtures`. |

## Key behaviours and rules

* **Event vs snapshot.** Event alerts fire on a false-to-true transition of an alert condition's AND-combined filters
  (gated by the strategy's own AND filters). Top Lists are periodic snapshots: rank by a numeric feature or formula
  (server-side), take the top N (default 100, refresh 30 s), then optionally apply a *display sort* to those rows.
  Ties break by symbol ascending so results are deterministic.
* **Determinism.** Event ids are `sha256(strategy|version|condition|symbol|source_ts|event_type)`. Replaying the same
  data yields identical event ids and results.
* **Config preservation.** Every alert carries the exact strategy configuration snapshot (with formula digests and a
  config hash); every backtest run stores the strategy snapshot and all parameters; strategy edits create new immutable
  versions.
* **No look-ahead.** Features use bars closed at `as_of`; `new_high` excludes the bar that produced `last`; RVOL
  baselines and previous close come from earlier sessions only.
* **Timestamps.** `source_timestamp` (provider) and `detected_timestamp` / `delivered_timestamp` (local) are separate;
  `stale_data` (data age at detection) and `delayed_delivery` (delivery latency) flags surface in the UI.
* **Backpressure.** Ticks drop-oldest when the bounded lane is full (counted); bars, session and provider events are
  never dropped; arrival order across lanes is preserved. WebSocket clients have bounded queues.
* **Corrections and late data.** A bar with a higher revision replaces the stored bar and accumulators are rebuilt; an
  older revision is ignored; a late (out-of-order) bar triggers a rebuild. Duplicates are no-ops.

## Fixture provider vs real provider

The fixture provider synthesises quotes/trades/bars from its bars and can inject faults (duplicates, a late bar, a
correction, a disconnect, a halt) for tests. The Alpaca adapter and `ResilientStream` are exercised by scripted fake
connections and an httpx mock transport, but **not** by a live feed here.

## Known limitations

* Features update on tick/bar arrival only; partial (in-progress) minute bars are not maintained.
* Open paper positions live in memory for the session; intents and fills are persisted.
* Halts from the Alpaca `s` status messages are recognised from the status text (`halt`, `resum`), not full status-code tables.
* One process; no clustering. SQLite file access is serialised by a lock.
* Relative volume uses up to 20 prior sessions held in memory per symbol (about 8k bars each).

## Sources

Trade Ideas (behavioural references only; no scraping, no undocumented APIs):

* Alert Window: https://www.trade-ideas.com/guide/chapter/9/9Alert_Window.html
* Top List Window: https://www.trade-ideas.com/guide/chapter/10/10Top_List_Window.html
* Sort Tab: https://www.trade-ideas.com/guide/chapter/10_2_7/10.2.7Sort_Tab.html
* Layouts & Scans: https://www.trade-ideas.com/guide/chapter/29/29Layouts_and_Scans.html
* Filter Codes: https://www.trade-ideas.com/AccountManagement/FilterCodes.html
* Formula Editor: https://www.trade-ideas.com/guide/chapter/8_3_3_1/8.3.3.1Formula_Editor.html
* Price Filter: https://www.trade-ideas.com/help/filter/Price/
* Backtesting/Oddsmaker: https://www.trade-ideas.com/guide/chapter/22/22Backtesting_Oddsmaker.html
* OddsMaker User's Guide v2: https://www.trade-ideas.com/OddsMaker/Help.version2.html
* Entry Tab: https://www.trade-ideas.com/guide/chapter/22_1/22.1Entry_Tab.html
* Timed Exit Tab: https://www.trade-ideas.com/guide/chapter/22_2/22.2Timed_Exit_Tab.html
* Risk Management Tab: https://www.trade-ideas.com/guide/chapter/22_3/22.3Risk_Management_Tab.html
* Advanced Exit Tab: https://www.trade-ideas.com/guide/chapter/22_4/22.4Advanced_Exit_Tab.html
* OddsMaker Backtesting Guide: https://www.trade-ideas.com/learning-center/backtesting-strategy-development/oddsmaker-backtesting-guide/
* How to Evaluate a Trading Strategy: https://www.trade-ideas.com/learning-center/backtesting-strategy-development/how-to-evaluate-a-trading-strategy/
* Holly AI signal generation: https://www.trade-ideas.com/learning-center/ai-in-trading/how-holly-ai-generates-trade-signals/
* Holly Windows: https://www.trade-ideas.com/hollyguide/Holly_Windows.html
* AI Holly Strategy Window: https://www.trade-ideas.com/hollyguide/AI_Holly_Strategy_Window.html
* AI/Holly Strategy Trades Window: https://www.trade-ideas.com/hollyguide/AI_Holly_Strategy_Trades_Window.html
* Show AI Trades: https://www.trade-ideas.com/guide/chapter/14_9_7/14.9.7Show_AI_Trades.html
* Audible Trade Notifications: https://www.trade-ideas.com/hollyguide/Trade_Notifications.html
* External Linking: https://www.trade-ideas.com/guide/chapter/8_3_2/8.3.2External_Linking.html
* One-Click Order Entry Template: https://www.trade-ideas.com/guide/chapter/21_4_2_1/21.4.2.1Create_a_OneClick_Order_Entry_Template_.html
* Auto-Trading Strategy guide: https://www.trade-ideas.com/guide/chapter/21_4_2_2/21.4.2.2Create_an_AutoTrading_Strategy_.html
* Stop/Limit/Stop-Market guide: https://www.trade-ideas.com/guide/chapter/21_4_2_3/21.4.2.3How_to_use_Stop_Limit_and_Stop_Market_Orders_as_Entry_Orders_.html
* Interactive Brokers connection: https://www.trade-ideas.com/guide/chapter/21_1_2/21.1.2Connect_to_Interactive_Brokers.html
* Features: https://www.trade-ideas.com/features/
* Brokers: https://www.trade-ideas.com/brokers/

Market data and paper environments:

* Alpaca real-time stock data: https://docs.alpaca.markets/docs/real-time-stock-pricing-data
* Alpaca WebSocket stream: https://docs.alpaca.markets/us/docs/streaming-market-data
* Alpaca paper trading: https://docs.alpaca.markets/docs/paper-trading
* Alpaca order structures: https://docs.alpaca.markets/docs/orders-at-alpaca
* Alpaca create-order reference: https://docs.alpaca.markets/us/reference/postorder
* IBKR historical bars: https://interactivebrokers.github.io/tws-api/historical_bars.html
* IBKR Web API: https://www.interactivebrokers.com/campus/ibkr-api-page/web-api-trading/
* IBKR paper-account limitations: https://www.interactivebrokers.com/campus/glossary-terms/paper-trading-account/
* TradeStation API: https://api.tradestation.com/docs/
* TradeStation API specification: https://api.tradestation.com/docs/specification/
* TradeStation SIM vs LIVE: https://api.tradestation.com/docs/fundamentals/sim-vs-live/
* Massive API documentation: https://massive.com/docs/
* Massive custom minute bars: https://massive.com/docs/rest/stocks/aggregates/custom-bars
* NYSE trading hours and calendars: https://www.nyse.com/markets/hours-calendars

How the sources were used (honest account): **none of the vendor or Trade Ideas pages above could be fetched from the build
environment** (the network egress proxy blocked those hosts). Behaviour was therefore taken from (1) the master build prompt,
(2) the uploaded `trade-ideas-technical-resource-map.md`, which summarises the Top List (30 s refresh, top-100 default, AND filters,
server sort vs display sort), Alert Window, OddsMaker (1-minute OHLC, regular session, one entry per symbol per day, daily trade
cap, entry/exit types, metrics) and the paper/broker guidance, and (3) general knowledge of the Alpaca streaming schema and the
NYSE calendar rules. Vendor schemas and calendar rules are therefore **implemented from the documented contract and not verified
against the live pages**; see [STATUS.md](STATUS.md). The order-related pages (One-Click, Auto-Trading, IBKR, TradeStation, Alpaca
orders) informed only what **not** to build: no order routing exists and the safety guard names their live hosts as forbidden.
The build order in the resource map (deterministic scanner core, alert service, backtester, paper forward validation) was followed.
