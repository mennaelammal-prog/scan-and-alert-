# Backtest methodology

> **Every backtest number is a historical simulation on 1-minute OHLC data. It is not a prediction and not a guarantee.**
> All fills, costs and balances are hypothetical. Nothing is ever sent to a broker.

The engine is `src/scanalert/backtest.py`. It is an **event-replay** backtester (not a bar-by-bar indicator tester): it
ingests an immutable chronological stream of 1-minute bars, runs the *same* `EventScanner`, `AlertManager` (dedupe/cooldown/daily
cap) and feature code as live scanning, and turns triggered alerts into simulated entries and exits.

## Replay loop (per trading day, per minute)

1. For each symbol with a bar this minute (sorted, deterministic): fill any pending entry at **this bar's open**.
2. Apply exit rules to open positions (priority below).
3. Ingest the closed bar into the symbol's state, optionally synthesise a quote (see spread), then evaluate the strategy at
   `as_of = bar start + 1 minute` (information available at that time only).
4. Route new alerts through the entry gates (window, one entry per symbol per day, daily cap, concurrent positions, daily loss limit).
5. Confirm/invalidate `working` alerts, evaluate alert-based exit filters (exit at the next bar's open).
6. At the end of each session close what the exit configuration requires; at the end of data flatten leftovers (`data_end`).

## 1-minute OHLC limitations (read this first)

* Bars carry no order-of-events inside the minute and no bid/ask. Anything that depends on tick sequence (queue position, exact
  stop/target order, latency) is assumed, not measured. This mirrors the documented limitation of OddsMaker-style testing
  (1-minute OHLC, not tick-by-tick).
* A signal computed at a bar's close cannot be traded at that exact close. The default entry model is therefore `next_open`.
  `alert_close` (fill at the alert bar's close) is available and is **optimistic**; the report says so.
* Halts, auctions, LULD pauses, partial fills and market impact are not modelled in backtests.

## Intrabar ambiguity policy

When one bar's range reaches both the stop and the profit target, the order is unknowable. The run's `intrabar_policy` decides and is
printed in the report and the assumptions list:

| Policy | Behaviour |
| --- | --- |
| `stop_first` (default, conservative) | Stop fills |
| `target_first` | Target fills |
| `reject_ambiguous` | The trade is **excluded** from all statistics and counted in `ambiguous_bars_excluded` / `skipped_signals` |

Other price rules: a bar that **opens beyond** a level fills at the **open** (gap-through), not at the level. Stops fill as market orders
(spread and slippage apply). Profit targets are limit orders (no spread/slippage unless `limit_target_costs=true`). Trailing stops trail
the high-water mark (percent of the HWM, or a fixed dollar distance) and the HWM is updated *after* the bar's stop check, so a single
bar's own extreme cannot both raise the stop and trigger it. In the entry bar, stop/target/trail may trigger after the open fill.

## Entries

* Entry direction: per strategy/condition, or forced `long`/`short`.
* Regular-session window (default 09:35-15:30 ET, configurable). Extended hours are rejected (`include_premarket/postmarket` cannot be enabled).
* `one_entry_per_symbol_per_day` (default on), `max_trades_per_day`, `max_concurrent_positions`, `daily_loss_limit`
  (blocks *new* entries once realised plus unrealised P&L for the day reaches the limit; open positions are not force-closed).
* Sizing: `fixed_shares`, `fixed_dollars`, `percent_equity`, `risk_percent` (risk budget / stop distance; needs a stop).
* Buying power: `equity * leverage - open notional`; quantity is reduced to fit (counted in "position reduced to fit buying power")
  or the signal is skipped. `buying_power_peak` is reported.

## Exits (evaluation priority within a bar)

1. Scheduled exits at the bar **open**: pending alert-based exit, `time_after_entry_minutes`, `time_of_day_exit` (entry day), `hold_days` with `multi_day_exit=open`.
2. Stop loss / trailing stop / profit target with the intrabar policy (dollar or percent).
3. End of session: `eod_close` (same-day close fallback, uses the early-close time on early-close days) when `hold_days=0`; with `hold_days=N` the
   position is closed at the open or close of the N-th following trading day (`multi_day_open` / `multi_day_close`), using the exchange calendar.
4. `data_end` if data stops while a position is open.

Alert-based exit: `exits.exit_filters` (AND) evaluated after each bar; the exit fills at the next bar's open.

## Spread, slippage and commission assumptions

* **Spread**: 1-minute bars have no quotes. `costs.spread_bps` is a *full-spread* assumption; half is paid on each market fill. Optionally
  `assumed_spread_bps` / `assumed_spread_bps_by_symbol` synthesise a quote at each bar close so `spread_bps` filters work; if neither is
  set, spread features are null and each filter's `null_policy` applies (documented in the report).
* **Slippage**: `slippage_bps` (default 2) or `slippage_cents`, adverse, per market fill.
* **Commission and fees**: `commission_per_share`, `min_commission_per_order`, `fee_bps` of notional per side. Defaults are zero; set them
  to your broker's schedule.
* Gross P&L uses reference prices (bar open/close/level); spread, slippage and commission are reported separately
  (`total_spread_cost`, `total_slippage_cost`, `total_commissions_fees`) and subtracted to get net P&L.

## Data coverage and provider

Each report states the provider, feed, adjustment mode, bar size, requested range, first/last simulated session, bars used vs expected,
overall coverage percent and coverage per symbol. **Always read the actual date range**: public materials about comparable tools give varying
history depths, and the fixture provider's 20 synthetic sessions are not real history. Missing minutes are simply skipped (no interpolation).

## Corporate action policy

The engine does not adjust prices. It relies on the provider's adjustment mode (`split` by default in the Alpaca request) and records it in the report.
Dividends, spin-offs, symbol changes and short-borrow costs are **not modelled**. A split inside a holding period on unadjusted data would produce a
false gap; use adjusted data for multi-day holds.

## Universe policy and survivorship

* `static`: the requested symbols for the whole range (default). This carries **survivorship bias** if the list is today's winners.
* `point_in_time`: `PointInTimeUniverse(snapshots, delisted)` resolves membership as of each trading day (latest snapshot on or before the date) and
  stops trading a symbol after its last tradable date. The backtest interface always takes a `Universe`, so point-in-time data can be plugged in.
  This repository does not ship historical constituent data; supplying it is the caller's responsibility.

## Session calendar policy

Sessions come from `NyseCalendar` (rules for holidays with observed dates, Good Friday, early closes at 13:00 ET, special closures, DST-correct
ET conversion). Weekdays are **not** assumed to be trading days. Rules are re-implemented from public NYSE schedules and should be re-verified
against https://www.nyse.com/markets/hours-calendars each year; `extra_holidays`/`extra_early_closes` patch the calendar without code changes.

## Look-ahead and leakage controls

* Signals use only bars closed at `as_of`; `new_high`/`new_low` exclude the bar that produced `last`.
* Entry defaults to the *next* bar's open.
* Relative-volume baselines use earlier sessions only; previous close is the prior session's regular close.
* Trailing-stop HWM updates after the check; the universe is resolved per day; strategy versions are frozen per run.
* Warm-up history before the start date is loaded only to initialise indicators, never traded.
* Remaining risk: parameters tuned on the same period are **in-sample**. Use walk-forward and out-of-sample splits (not automated here).

## Paper-fill limitations

Live-style paper intents use a different, simpler simulator (`paper.py`): next trade, next bar open, bid/ask cross, fixed slippage,
spread plus slippage, partial fills limited by size or 10 percent of bar volume, rejection on stale data or thin liquidity. It has no queue,
latency or impact model, and its results should not be compared with a real paper broker without a drift study. Backtest fills and paper fills
share the collision policy but not the cost model.

## Reported metrics

Total trades, win rate, profit factor (undefined without losers, reported as null with a note), expectancy per trade, average winner/loser,
gross and net P&L, maximum drawdown (closed-trade equity plus end-of-day mark-to-market, dollars and percent), equity curve, drawdown series,
daily P&L, trades per day, buying-power peak, holding-time distribution and mean/median, exit-reason distribution, consecutive wins/losses,
spread/slippage/commission totals, filter and feature attribution (average observed value for winners vs losers at entry; by symbol and condition),
skipped-signal reasons, suppressed alert count, data coverage, strategy version and config hash, provider and feed, and the full assumptions list.

## Not implemented (be honest about it)

Parameter sensitivity sweeps, walk-forward evaluation, Monte Carlo reshuffling, regime splits and latency-sensitivity runs are recommended by the
reference material but are **not** part of this build. `time_after_entry` is exact-minute (OddsMaker rounds to five minutes).

## Reproducing a run

The report embeds `config` (all parameters), `strategy.config_hash` and the assumptions; `backtest_runs.strategy_snapshot` stores the exact strategy.
With the same data and the same code the result is bit-for-bit deterministic (tested).
