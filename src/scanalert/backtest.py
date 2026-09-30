"""OddsMaker-style event-replay backtester over 1-minute bars.

Everything here is a HISTORICAL SIMULATION on 1-minute OHLC data. It is not a prediction, not a
guarantee, and it never touches a broker. The same :class:`~scanalert.scanner.EventScanner`,
:class:`~scanalert.alerts.AlertManager` and feature code used for live scanning generate the signals.

No-look-ahead rules: a signal is evaluated only on bars that have closed (``as_of = bar start + 1m``);
by default the entry fills at the *next* bar's open; relative-volume baselines and previous close come
from earlier sessions only; the universe is resolved as-of each trading day.
"""

from __future__ import annotations

import math
import statistics
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .alerts import AlertEvent, AlertManager
from .calendar import ET, NyseCalendar, et_date
from .features import KEEP_DAYS, SymbolState
from .filters import EvalContext, FilterSpec, evaluate_all
from .models import Bar, Quote, iso
from .paper import IntrabarPolicy, resolve_stop_target
from .scanner import EventScanner
from .strategy import StrategySpec

DISCLAIMER = (
    "HISTORICAL SIMULATION on 1-minute OHLC bars. Not a prediction or guarantee of future results. "
    "All fills, costs and balances are hypothetical."
)


# ================================================================================ configuration
class LevelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["percent", "dollars"] = "percent"
    value: float = Field(gt=0)


class SizingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["fixed_shares", "fixed_dollars", "percent_equity", "risk_percent"] = "fixed_dollars"
    shares: int = Field(100, ge=1)
    dollars: float = Field(5_000.0, gt=0)
    percent: float = Field(5.0, gt=0, le=100)
    risk_percent: float = Field(0.5, gt=0, le=10)


class ExitConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profit_target: LevelSpec | None = None
    stop_loss: LevelSpec | None = None
    trailing_stop: LevelSpec | None = None
    time_after_entry_minutes: int | None = Field(None, ge=1, le=390)
    time_of_day_exit: str | None = None  # "HH:MM" ET
    hold_days: int = Field(0, ge=0, le=20)  # 0 = same-day close fallback
    multi_day_exit: Literal["open", "close"] = "close"  # exit point on day entry_day + hold_days
    exit_filters: list[FilterSpec] = Field(
        default_factory=list
    )  # alert-based exit (AND); exits at next bar open

    @model_validator(mode="after")
    def _v(self) -> ExitConfig:
        if self.time_of_day_exit:
            try:
                time.fromisoformat(self.time_of_day_exit)
            except ValueError as exc:
                raise ValueError("time_of_day_exit must be HH:MM") from exc
        return self


class CostConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commission_per_share: float = Field(0.0, ge=0)
    min_commission_per_order: float = Field(0.0, ge=0)
    fee_bps: float = Field(0.0, ge=0)  # on notional, per side
    spread_bps: float = Field(5.0, ge=0)  # full quoted spread assumption; half paid per market fill
    slippage_bps: float = Field(2.0, ge=0)  # adverse, per market fill
    slippage_cents: float | None = Field(None, ge=0)  # overrides slippage_bps when set
    limit_target_costs: bool = False  # target exits are limit orders: no spread/slippage unless True


class BacktestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_date: date
    end_date: date
    symbols: list[str] | None = None
    universe_mode: Literal["static", "point_in_time"] = "static"
    direction: Literal["strategy", "long", "short"] = "strategy"
    entry_price_model: Literal["next_open", "alert_close"] = "next_open"
    entry_start: str = "09:35"
    entry_end: str = "15:30"
    one_entry_per_symbol_per_day: bool = True
    max_trades_per_day: int | None = Field(20, ge=1)
    max_concurrent_positions: int = Field(5, ge=1)
    daily_loss_limit: float | None = Field(None, gt=0)
    starting_equity: float = Field(100_000.0, gt=0)
    leverage: float = Field(1.0, gt=0, le=6)
    sizing: SizingConfig = Field(default_factory=SizingConfig)
    exits: ExitConfig = Field(default_factory=ExitConfig)
    costs: CostConfig = Field(default_factory=CostConfig)
    intrabar_policy: IntrabarPolicy = "stop_first"
    include_premarket: bool = False
    include_postmarket: bool = False
    # 1-minute bars carry no bid/ask. Optionally synthesise a quote at each bar close so spread filters
    # work; per-symbol values override the default. Left unset, spread features are null (null_policy applies).
    assumed_spread_bps: float | None = Field(None, ge=0)
    assumed_spread_bps_by_symbol: dict[str, float] = Field(default_factory=dict)
    warmup_days: int = Field(KEEP_DAYS - 1, ge=0, le=60)

    @model_validator(mode="after")
    def _v(self) -> BacktestConfig:
        if self.end_date < self.start_date:
            raise ValueError("end_date must not precede start_date")
        if self.include_premarket or self.include_postmarket:
            raise ValueError(
                "extended-hours backtests are not supported (OddsMaker-style: regular session only)"
            )
        if self.sizing.mode == "risk_percent" and self.exits.stop_loss is None:
            raise ValueError("risk_percent sizing requires exits.stop_loss")
        if time.fromisoformat(self.entry_start) >= time.fromisoformat(self.entry_end):
            raise ValueError("entry_start must be before entry_end")
        if self.symbols is not None:
            self.symbols = sorted({s.strip().upper() for s in self.symbols if s.strip()})
        return self


# =================================================================================== universes
class Universe(Protocol):
    def symbols_on(self, d: date) -> list[str]: ...


@dataclass
class StaticUniverse:
    symbols: list[str]

    def symbols_on(self, d: date) -> list[str]:
        return sorted(self.symbols)


@dataclass
class PointInTimeUniverse:
    """Membership snapshots effective from a date; the latest snapshot on/before ``d`` applies.

    ``delisted`` maps symbol -> last tradable date (inclusive) to avoid survivorship bias.
    """

    snapshots: dict[date, list[str]]
    delisted: dict[str, date] = field(default_factory=dict)

    def symbols_on(self, d: date) -> list[str]:
        eff = [x for x in self.snapshots if x <= d]
        if not eff:
            return []
        syms = self.snapshots[max(eff)]
        return sorted(s for s in syms if s not in self.delisted or d <= self.delisted[s])


# ================================================================================== internals
@dataclass
class _Pos:
    symbol: str
    direction: str
    qty: float
    entry_ts: datetime
    entry_ref: float
    entry_fill: float
    stop: float | None
    target: float | None
    trail: LevelSpec | None
    hwm: float
    entry_day: date
    entry_costs: dict[str, float]
    event_id: str
    condition_id: str
    features: dict[str, Any]
    filters: dict[str, Any]
    notional: float
    pending_exit: str | None = None
    last_close: float = 0.0
    check_entry_bar: bool = False  # next_open entries: stop/target may hit inside the entry bar


@dataclass
class _Pending:
    alert: AlertEvent
    direction: str
    scheduled_at: datetime
    features: dict[str, Any]
    filters: dict[str, Any]


@dataclass
class BacktestResult:
    config: BacktestConfig
    trades: list[dict[str, Any]]
    report: dict[str, Any]


def _cost_fill(
    cfg: CostConfig, price: float, qty: float, side: str, limit: bool
) -> tuple[float, float, float, float]:
    """Returns (fill_price, spread_cost, slippage_cost, commission_and_fees) for one order."""
    half = 0.0 if limit and not cfg.limit_target_costs else price * cfg.spread_bps / 20_000
    slip = (
        0.0
        if limit and not cfg.limit_target_costs
        else (
            cfg.slippage_cents / 100 if cfg.slippage_cents is not None else price * cfg.slippage_bps / 10_000
        )
    )
    sgn = 1 if side == "buy" else -1
    fill = price + sgn * (half + slip)
    commission = 0.0
    if cfg.commission_per_share or cfg.min_commission_per_order:
        commission = max(cfg.min_commission_per_order, qty * cfg.commission_per_share)
    commission += fill * qty * cfg.fee_bps / 10_000
    return fill, half * qty, slip * qty, commission


def _trail_level(p: _Pos) -> float | None:
    """Trailing stop level from the high-water mark (percent trails the HWM, dollars is a fixed distance)."""
    if p.trail is None:
        return None
    sgn = 1 if p.direction == "long" else -1
    dist = p.hwm * p.trail.value / 100 if p.trail.type == "percent" else p.trail.value
    return p.hwm - sgn * dist


def _tod(bar_ts: datetime) -> time:
    return bar_ts.astimezone(ET).time()


class _Book:
    """Bookkeeping shared by the replay loop."""

    def __init__(self, cfg: BacktestConfig):
        self.cfg = cfg
        self.realized = 0.0
        self.trades: list[dict[str, Any]] = []
        self.open: dict[str, _Pos] = {}
        self.pending: dict[str, _Pending] = {}
        self.entries_by_day: dict[date, int] = defaultdict(int)
        self.entered_today: dict[date, set[str]] = defaultdict(set)
        self.realized_by_day: dict[date, float] = defaultdict(float)
        self.skipped: dict[str, int] = defaultdict(int)
        self.ambiguous: list[dict[str, Any]] = []
        self.bp_peak = 0.0
        self.equity_curve: list[tuple[datetime, float]] = []
        self.marks: dict[str, float] = {}

    def equity(self) -> float:
        return self.cfg.starting_equity + self.realized

    def unrealized(self) -> float:
        u = 0.0
        for p in self.open.values():
            m = self.marks.get(p.symbol, p.entry_fill)
            u += (1 if p.direction == "long" else -1) * (m - p.entry_ref) * p.qty
        return u

    def open_notional(self) -> float:
        return sum(p.notional for p in self.open.values())


def run_backtest(
    strategy: StrategySpec,
    cfg: BacktestConfig,
    bars: dict[str, list[Bar]],
    calendar: NyseCalendar | None = None,
    universe: Universe | None = None,
    provider: str = "",
    feed: str = "",
    adjustment: str = "as-provided",
) -> BacktestResult:
    """Replay ``bars`` (sorted 1-minute bars, warm-up history included) through the strategy."""
    cal = calendar or NyseCalendar()
    syms_all = sorted(bars)
    if universe is None:
        universe = StaticUniverse(cfg.symbols or strategy.universe or syms_all)
    days = cal.trading_days(cfg.start_date, cfg.end_date)
    by_day: dict[str, dict[date, dict[datetime, Bar]]] = {}
    for s, bs in bars.items():
        dd: dict[date, dict[datetime, Bar]] = defaultdict(dict)
        for b in bs:
            dd[et_date(b.ts)][b.ts] = b
        by_day[s] = dd

    states = {s: SymbolState(s, cal) for s in syms_all}
    warm_start = cfg.start_date - timedelta(days=cfg.warmup_days * 2 + 4)
    for s in syms_all:  # seed warm-up sessions strictly before the test window
        for d in sorted(x for x in by_day[s] if warm_start <= x < cfg.start_date):
            if cal.is_trading_day(d):
                for ts in sorted(by_day[s][d]):
                    states[s].on_bar(by_day[s][d][ts])

    scanner = EventScanner(cal)
    sim_now = [datetime.min.replace(tzinfo=ET)]
    manager = AlertManager(store=None, clock=lambda: sim_now[0], provider=provider, feed=feed)
    book = _Book(cfg)
    tod_exit = time.fromisoformat(cfg.exits.time_of_day_exit) if cfg.exits.time_of_day_exit else None
    coverage: dict[str, dict[str, Any]] = {
        s: {"days_with_data": 0, "bars": 0, "expected": 0} for s in syms_all
    }
    exit_day_of: dict[str, date] = {}
    waiting: dict[str, _Pending] = {}
    signals_seen = 0

    def close_pos(p: _Pos, ts: datetime, ref_price: float, reason: str, limit: bool = False) -> None:
        side = "sell" if p.direction == "long" else "buy"
        fill, spread_c, slip_c, comm = _cost_fill(cfg.costs, ref_price, p.qty, side, limit)
        sgn = 1 if p.direction == "long" else -1
        gross = sgn * (ref_price - p.entry_ref) * p.qty
        spread_total = p.entry_costs["spread"] + spread_c
        slip_total = p.entry_costs["slippage"] + slip_c
        comm_total = p.entry_costs["commission"] + comm
        net = gross - spread_total - slip_total - comm_total
        book.realized += net
        book.realized_by_day[et_date(ts)] += net
        rec = {
            "trade_no": len(book.trades) + 1,
            "symbol": p.symbol,
            "direction": p.direction,
            "event_id": p.event_id,
            "condition_id": p.condition_id,
            "entry_ts": iso(p.entry_ts),
            "entry_price": round(p.entry_fill, 4),
            "entry_ref_price": round(p.entry_ref, 4),
            "exit_ts": iso(ts),
            "exit_price": round(fill, 4),
            "exit_ref_price": round(ref_price, 4),
            "quantity": p.qty,
            "gross_pnl": round(gross, 4),
            "spread_cost": round(spread_total, 4),
            "slippage_cost": round(slip_total, 4),
            "commission": round(comm_total, 4),
            "costs": round(spread_total + slip_total + comm_total, 4),
            "net_pnl": round(net, 4),
            "return_pct": round(net / p.notional * 100, 4) if p.notional else 0.0,
            "exit_reason": reason,
            "holding_minutes": round((ts - p.entry_ts).total_seconds() / 60, 2),
            "features_at_entry": p.features,
            "filters_at_entry": p.filters,
            "simulated": True,
        }
        book.trades.append(rec)
        del book.open[p.symbol]
        book.equity_curve.append((ts, book.equity()))

    def open_pos(pend: _Pending, ref: float, ts: datetime, at_open: bool) -> bool:
        d = et_date(ts)
        direction = pend.direction
        side = "buy" if direction == "long" else "sell"
        est_fill, _, _, _ = _cost_fill(cfg.costs, ref, 1, side, False)
        stop_dist = None
        sl = cfg.exits.stop_loss
        if sl:
            stop_dist = est_fill * sl.value / 100 if sl.type == "percent" else sl.value
        z = cfg.sizing
        if z.mode == "fixed_shares":
            qty = float(z.shares)
        elif z.mode == "fixed_dollars":
            qty = math.floor(z.dollars / ref)
        elif z.mode == "percent_equity":
            qty = math.floor(book.equity() * z.percent / 100 / ref)
        else:
            qty = math.floor(book.equity() * z.risk_percent / 100 / stop_dist) if stop_dist else 0
        bp_avail = book.equity() * cfg.leverage - book.open_notional()
        if qty * ref > bp_avail:
            qty = math.floor(bp_avail / ref) if bp_avail > 0 else 0
            if qty >= 1:
                book.skipped["position reduced to fit buying power"] += 1
        if qty < 1:
            book.skipped["insufficient buying power / size below 1 share"] += 1
            return False
        if (
            cfg.daily_loss_limit is not None
            and book.realized_by_day[d] + book.unrealized() <= -cfg.daily_loss_limit
        ):
            book.skipped["daily loss limit"] += 1
            return False
        fill, spread_c, slip_c, comm = _cost_fill(cfg.costs, ref, qty, side, False)
        sgn = 1 if direction == "long" else -1
        stop = fill - sgn * stop_dist if stop_dist else None
        tp = cfg.exits.profit_target
        target = None
        if tp:
            target = fill + sgn * (fill * tp.value / 100 if tp.type == "percent" else tp.value)
        trail = cfg.exits.trailing_stop
        p = _Pos(
            pend.alert.symbol,
            direction,
            qty,
            ts,
            ref,
            fill,
            stop,
            target,
            trail,
            fill,
            d,
            {"spread": spread_c, "slippage": slip_c, "commission": comm},
            pend.alert.event_id,
            pend.alert.condition_id,
            pend.features,
            pend.filters,
            qty * ref,
            check_entry_bar=at_open,
        )
        book.open[p.symbol] = p
        book.entries_by_day[d] += 1
        book.entered_today[d].add(p.symbol)
        book.bp_peak = max(book.bp_peak, book.open_notional())
        if cfg.exits.hold_days > 0:
            exit_day_of[p.symbol] = _add_trading_days(cal, d, cfg.exits.hold_days)
        return True

    def eval_position_bar(p: _Pos, bar: Bar) -> None:
        """Apply exit rules for one bar in fixed priority order."""
        ts = bar.ts
        d = et_date(ts)
        exit_open_reason = None
        if p.pending_exit:
            exit_open_reason = p.pending_exit
        elif cfg.exits.time_after_entry_minutes and ts >= p.entry_ts + timedelta(
            minutes=cfg.exits.time_after_entry_minutes
        ):
            exit_open_reason = "time_exit"
        elif tod_exit and d == p.entry_day and _tod(ts) >= tod_exit and _tod(p.entry_ts) < tod_exit:
            exit_open_reason = "time_of_day_exit"
        elif p.symbol in exit_day_of and d >= exit_day_of[p.symbol] and cfg.exits.multi_day_exit == "open":
            exit_open_reason = "multi_day_open"
        if (
            exit_open_reason and ts > p.entry_ts
        ):  # cannot exit before the bar after entry, except entry-bar rules below
            close_pos(p, ts, bar.open, exit_open_reason)
            return
        sgn = 1 if p.direction == "long" else -1
        eff_stop, stop_tag = p.stop, "stop_loss"
        tl = _trail_level(p)
        if tl is not None and (eff_stop is None or sgn * (tl - eff_stop) > 0):
            eff_stop, stop_tag = tl, "trailing_stop"
        res = resolve_stop_target(p.direction, eff_stop, p.target, bar, cfg.intrabar_policy)
        if res == "ambiguous":
            book.ambiguous.append({"symbol": p.symbol, "ts": iso(ts), "stop": eff_stop, "target": p.target})
            book.skipped["trade excluded: ambiguous stop/target bar"] += 1
            del book.open[p.symbol]  # excluded from statistics by policy (reject-as-ambiguous)
            return
        if res is not None:
            reason, price = res
            if reason == "stop":
                close_pos(p, ts, price, stop_tag)
            else:
                close_pos(p, ts, price, "profit_target", limit=True)
            return
        # update trailing high-water mark AFTER the stop check (no same-bar look-ahead)
        p.hwm = max(p.hwm, bar.high) if p.direction == "long" else min(p.hwm, bar.low)
        p.last_close = bar.close

    for d in days:
        minutes = cal.regular_minutes(d)
        if not minutes:
            continue
        uni = set(universe.symbols_on(d)) & set(syms_all)
        book.entered_today[d]  # ensure key
        for s in syms_all:
            if s in uni:
                coverage[s]["expected"] += len(minutes)
                n = len(by_day[s].get(d, {}))
                coverage[s]["bars"] += n
                coverage[s]["days_with_data"] += 1 if n else 0

        def flush_alert_close(m: datetime, as_of: datetime, d: date = d) -> None:
            if cfg.entry_price_model != "alert_close":
                return
            for sym in list(book.pending):
                pend = book.pending.pop(sym)
                b = by_day[sym].get(d, {}).get(m)
                if b is None:
                    book.skipped["no bar at alert time"] += 1
                    continue
                open_pos(pend, b.close, as_of, at_open=False)

        for m in minutes:
            as_of = m + timedelta(minutes=1)
            sim_now[0] = as_of
            active = sorted(uni | set(book.open))
            for s in active:
                bar = by_day[s].get(d, {}).get(m)
                if bar is None:
                    continue
                # (1) fill a pending entry at this bar's open
                pend = book.pending.pop(s, None)
                if pend is not None:
                    open_pos(pend, bar.open, m, at_open=True)
                # (2) exits for open positions (includes a position entered at this open)
                p = book.open.get(s)
                if p is not None:
                    if p.check_entry_bar:
                        p.check_entry_bar = False
                        _entry_bar_check(p, bar, cfg, book, close_pos)
                    else:
                        eval_position_bar(p, bar)
                book.marks[s] = bar.close
                # (3) ingest the closed bar, then evaluate signals on information available at as_of
                states[s].on_bar(bar)
                if s in uni:
                    sp = cfg.assumed_spread_bps_by_symbol.get(s, cfg.assumed_spread_bps)
                    if sp is not None:
                        half = max(0.005, bar.close * sp / 20_000)
                        states[s].on_quote(
                            Quote(s, as_of, round(bar.close - half, 4), round(bar.close + half, 4), 0.0, 0.0)
                        )
                    for sig in scanner.evaluate(strategy, states[s], as_of, uni):
                        signals_seen += 1
                        alert = manager.submit(sig)
                        _route_alert(alert, sig, m, d, cfg, book, waiting, cal, as_of)
            # confirmation handling for working alerts
            if waiting:

                def _still(a: AlertEvent, at: datetime = as_of) -> bool:
                    return (
                        scanner.still_true(strategy, a.condition_id, states[a.symbol], at)
                        if a.symbol in states
                        else False
                    )

                changed = manager.advance(as_of, _still)
                for a in changed:
                    w = waiting.pop(a.event_id, None)
                    if w is not None and a.status == "triggered":
                        _schedule(a, w, d, m, cfg, book, cal, as_of)
            flush_alert_close(m, as_of)
            # alert-based exit evaluation (after bar close, exit at next bar open)
            if cfg.exits.exit_filters:
                for s, p in list(book.open.items()):
                    if p.pending_exit is None and s in states:
                        ok, _ = evaluate_all(
                            cfg.exits.exit_filters, EvalContext(states[s], as_of), short_circuit=True
                        )
                        if ok:
                            p.pending_exit = "alert_exit"
        # ---- end of session handling
        for s, p in list(book.open.items()):
            lb = by_day[s].get(d)
            last = lb[max(lb)] if lb else None
            exit_day = exit_day_of.get(s)
            if last is None:
                continue
            if cfg.exits.hold_days == 0:
                close_pos(p, last.ts + timedelta(minutes=1), last.close, "eod_close")
            elif exit_day is not None and d >= exit_day and cfg.exits.multi_day_exit == "close":
                close_pos(p, last.ts + timedelta(minutes=1), last.close, "multi_day_close")
        for s in list(book.pending):  # no next bar today: entry cannot fill
            del book.pending[s]
            book.skipped["signal at last bar of session: no next bar to fill"] += 1
        waiting.clear()
        book.equity_curve.append((cal.close_dt(d), book.equity() + book.unrealized()))

    # data_end: flatten anything still open at the last known close
    for s, p in list(book.open.items()):
        sym_days = by_day[s]
        last_day = max((x for x in sym_days if x <= cfg.end_date), default=None)
        if last_day is None:
            continue
        last = sym_days[last_day][max(sym_days[last_day])]
        close_pos(p, last.ts + timedelta(minutes=1), last.close, "data_end")

    report = build_report(
        strategy, cfg, book, coverage, days, provider, feed, adjustment, signals_seen, manager
    )
    return BacktestResult(cfg, book.trades, report)


def _entry_bar_check(p: _Pos, bar: Bar, cfg: BacktestConfig, book: _Book, close_pos: Any) -> None:
    """Exit logic for the bar in which the entry filled at the open."""
    sgn = 1 if p.direction == "long" else -1
    eff_stop, tag = p.stop, "stop_loss"
    tl = _trail_level(p)
    if tl is not None and (eff_stop is None or sgn * (tl - eff_stop) > 0):
        eff_stop, tag = tl, "trailing_stop"
    # entered at the open, so a gap-through level cannot apply; evaluate range only
    probe = Bar(bar.symbol, bar.ts, p.entry_fill, bar.high, bar.low, bar.close, bar.volume)
    if probe.high < p.entry_fill or probe.low > p.entry_fill:
        probe = Bar(
            bar.symbol,
            bar.ts,
            min(max(p.entry_fill, bar.low), bar.high),
            bar.high,
            bar.low,
            bar.close,
            bar.volume,
        )
    res = resolve_stop_target(p.direction, eff_stop, p.target, probe, cfg.intrabar_policy)
    if res == "ambiguous":
        book.ambiguous.append({"symbol": p.symbol, "ts": iso(bar.ts), "stop": eff_stop, "target": p.target})
        book.skipped["trade excluded: ambiguous stop/target bar"] += 1
        del book.open[p.symbol]
        return
    if res is not None:
        reason, price = res
        if reason == "stop":
            close_pos(p, bar.ts, price, tag)
        else:
            close_pos(p, bar.ts, price, "profit_target", limit=True)
        return
    p.hwm = max(p.hwm, bar.high) if p.direction == "long" else min(p.hwm, bar.low)
    p.last_close = bar.close


def _add_trading_days(cal: NyseCalendar, d: date, n: int) -> date:
    for _ in range(n):
        d = cal.next_trading_day(d)
    return d


def _route_alert(
    alert: AlertEvent,
    sig: Any,
    m: datetime,
    d: date,
    cfg: BacktestConfig,
    book: _Book,
    waiting: dict[str, _Pending],
    cal: NyseCalendar,
    as_of: datetime,
) -> None:
    if alert.status == "suppressed":
        book.skipped[f"alert suppressed ({alert.status_reason.split(':')[0]})"] += 1
        return
    direction = sig.direction if cfg.direction == "strategy" else cfg.direction
    pend = _Pending(alert, direction, as_of, sig.feature_snapshot, _compact_filters(sig.filter_snapshot))
    if alert.status == "working":
        waiting[alert.event_id] = pend
        return
    _schedule(alert, pend, d, m, cfg, book, cal, as_of)


def _compact_filters(fs: dict[str, Any]) -> dict[str, Any]:
    return {
        "strategy_id": fs.get("strategy_id"),
        "strategy_version": fs.get("strategy_version"),
        "condition_id": fs.get("condition_id"),
        "strategy_filters": fs.get("strategy_filters"),
        "condition_filters": fs.get("condition_filters"),
    }


def _schedule(
    alert: AlertEvent,
    pend: _Pending,
    d: date,
    m: datetime,
    cfg: BacktestConfig,
    book: _Book,
    cal: NyseCalendar,
    as_of: datetime,
) -> None:
    reason = book_can_enter(book, cfg, alert.symbol, d, as_of)
    if reason:
        book.skipped[reason] += 1
        return
    is_last_bar = m == cal.regular_minutes(d)[-1]
    if cfg.entry_price_model == "alert_close":
        book.pending[alert.symbol] = pend  # filled at the alert bar's close by flush_alert_close
        return
    if is_last_bar:
        book.skipped["signal at last bar of session: no next bar to fill"] += 1
        return
    book.pending[alert.symbol] = pend


def book_can_enter(book: _Book, cfg: BacktestConfig, sym: str, d: date, at: datetime) -> str | None:
    entry_start, entry_end = time.fromisoformat(cfg.entry_start), time.fromisoformat(cfg.entry_end)
    t = at.astimezone(ET).time()
    if not (entry_start <= t <= entry_end):
        return "outside entry window"
    if cfg.one_entry_per_symbol_per_day and sym in book.entered_today[d]:
        return "one entry per symbol per day"
    if sym in book.open or sym in book.pending:
        return "position already open"
    if (
        cfg.max_trades_per_day is not None
        and book.entries_by_day[d] + len(book.pending) >= cfg.max_trades_per_day
    ):
        return "daily trade cap"
    if len(book.open) + len(book.pending) >= cfg.max_concurrent_positions:
        return "max concurrent positions"
    if (
        cfg.daily_loss_limit is not None
        and book.realized_by_day[d] + book.unrealized() <= -cfg.daily_loss_limit
    ):
        return "daily loss limit"
    return None


# ================================================================================== reporting
def _dist(values: list[float], edges: list[float], labels: list[str]) -> dict[str, int]:
    out = {lab: 0 for lab in labels}
    for v in values:
        for i, e in enumerate(edges):
            if v < e:
                out[labels[i]] += 1
                break
        else:
            out[labels[-1]] += 1
    return out


def _max_streak(flags: list[bool], target: bool) -> int:
    best = cur = 0
    for f in flags:
        cur = cur + 1 if f == target else 0
        best = max(best, cur)
    return best


def build_report(
    strategy: StrategySpec,
    cfg: BacktestConfig,
    book: _Book,
    coverage: dict[str, dict[str, Any]],
    days: list[date],
    provider: str,
    feed: str,
    adjustment: str,
    signals_seen: int,
    manager: AlertManager,
) -> dict[str, Any]:
    trades = book.trades
    nets = [t["net_pnl"] for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    n = len(trades)
    gross_win, gross_loss = sum(wins), -sum(losses)
    # equity curve + drawdown (closed-trade and end-of-day mark points)
    curve = [(cfg.start_date.isoformat() + "T00:00:00Z", cfg.starting_equity)] + [
        (iso(ts), round(e, 2)) for ts, e in book.equity_curve
    ]
    peak, max_dd, max_dd_pct = cfg.starting_equity, 0.0, 0.0
    dd_series: list[dict[str, Any]] = []
    for ts, e in curve:
        peak = max(peak, e)
        dd = peak - e
        max_dd = max(max_dd, dd)
        max_dd_pct = max(max_dd_pct, dd / peak * 100 if peak else 0)
        dd_series.append({"ts": ts, "equity": e, "drawdown": round(dd, 2)})
    daily: dict[str, float] = defaultdict(float)
    per_day_count: dict[str, int] = defaultdict(int)
    for t in trades:
        daily[t["exit_ts"][:10]] += t["net_pnl"]
        per_day_count[t["entry_ts"][:10]] += 1
    hold = [t["holding_minutes"] for t in trades]
    reasons: dict[str, int] = defaultdict(int)
    for t in trades:
        reasons[t["exit_reason"]] += 1

    def _bucket(key: str) -> dict[str, dict[str, Any]]:
        g: dict[str, list[float]] = defaultdict(list)
        for t in trades:
            g[str(t[key])].append(t["net_pnl"])
        return {
            k: {
                "trades": len(v),
                "net_pnl": round(sum(v), 2),
                "win_rate": round(sum(1 for x in v if x > 0) / len(v), 4),
            }
            for k, v in sorted(g.items())
        }

    attribution = _attribution(trades)
    cov_rows = {}
    for s, c in coverage.items():
        if c["expected"]:
            cov_rows[s] = {**c, "coverage_pct": round(c["bars"] / c["expected"] * 100, 2)}
    total_exp = sum(c["expected"] for c in coverage.values())
    total_bars = sum(c["bars"] for c in coverage.values())
    return {
        "label": DISCLAIMER,
        "strategy": {
            "id": strategy.id,
            "version": strategy.version,
            "name": strategy.name,
            "config_hash": strategy.config_snapshot()["config_hash"],
        },
        "data": {
            "provider": provider,
            "feed": feed,
            "adjustment": adjustment,
            "bar_size": "1Min",
            "requested_range": [cfg.start_date.isoformat(), cfg.end_date.isoformat()],
            "trading_days_in_range": len(days),
            "first_trading_day": days[0].isoformat() if days else None,
            "last_trading_day": days[-1].isoformat() if days else None,
            "bars_used": total_bars,
            "bars_expected": total_exp,
            "overall_coverage_pct": round(total_bars / total_exp * 100, 2) if total_exp else 0.0,
            "per_symbol": cov_rows,
        },
        "metrics": {
            "total_trades": n,
            "winners": len(wins),
            "losers": len(losses),
            "win_rate": round(len(wins) / n, 4) if n else None,
            "profit_factor": round(gross_win / gross_loss, 4) if gross_loss > 0 else None,
            "profit_factor_note": None
            if gross_loss > 0
            else "undefined: no losing trades"
            if n
            else "no trades",
            "expectancy": round(sum(nets) / n, 4) if n else None,
            "average_winner": round(statistics.fmean(wins), 4) if wins else None,
            "average_loser": round(statistics.fmean(losses), 4) if losses else None,
            "gross_pnl": round(sum(t["gross_pnl"] for t in trades), 2),
            "net_pnl": round(sum(nets), 2),
            "return_pct": round(sum(nets) / cfg.starting_equity * 100, 4),
            "starting_equity": cfg.starting_equity,
            "ending_equity": round(cfg.starting_equity + sum(nets), 2),
            "max_drawdown": round(max_dd, 2),
            "max_drawdown_pct": round(max_dd_pct, 4),
            "drawdown_basis": "closed-trade equity plus end-of-day mark-to-market",
            "buying_power_peak": round(book.bp_peak, 2),
            "avg_trades_per_day": round(n / len(days), 4) if days else 0.0,
            "max_consecutive_wins": _max_streak([x > 0 for x in nets], True),
            "max_consecutive_losses": _max_streak([x < 0 for x in nets], True) if n else 0,
            "total_commissions_fees": round(sum(t["commission"] for t in trades), 2),
            "total_spread_cost": round(sum(t["spread_cost"] for t in trades), 2),
            "total_slippage_cost": round(sum(t["slippage_cost"] for t in trades), 2),
            "total_costs": round(sum(t["costs"] for t in trades), 2),
            "ambiguous_bars_excluded": len(book.ambiguous),
            "signals_detected": signals_seen,
        },
        "equity_curve": [{"ts": ts, "equity": e} for ts, e in curve],
        "drawdown_series": dd_series,
        "daily_pnl": [{"date": k, "net_pnl": round(v, 2)} for k, v in sorted(daily.items())],
        "trades_per_day": dict(sorted(per_day_count.items())),
        "holding_time": {
            "mean_minutes": round(statistics.fmean(hold), 2) if hold else None,
            "median_minutes": round(statistics.median(hold), 2) if hold else None,
            "distribution": _dist(
                hold,
                [5, 15, 30, 60, 120, 390],
                ["<5m", "5-15m", "15-30m", "30-60m", "1-2h", "2h-1d", ">=1 session"],
            ),
        },
        "exit_reasons": dict(sorted(reasons.items())),
        "skipped_signals": dict(sorted(book.skipped.items())),
        "suppressed_alerts": sum(1 for a in manager.alerts.values() if a.status == "suppressed"),
        "attribution": {
            **attribution,
            "by_symbol": _bucket("symbol"),
            "by_condition": _bucket("condition_id"),
        },
        "intrabar_policy": cfg.intrabar_policy,
        "assumptions": assumptions_list(cfg, provider, feed, adjustment),
        "config": cfg.model_dump(mode="json"),
    }


def _attribution(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-filter view: average observed value for winners vs losers at entry."""
    acc: dict[str, dict[str, Any]] = {}
    for t in trades:
        fl = t.get("filters_at_entry") or {}
        for r in [*(fl.get("strategy_filters") or []), *(fl.get("condition_filters") or [])]:
            a = acc.setdefault(
                r["filter_id"],
                {
                    "field": r["field"],
                    "operator": r["operator"],
                    "threshold": r["threshold"],
                    "w": [],
                    "l": [],
                    "n": 0,
                },
            )
            a["n"] += 1
            v = r.get("observed")
            if isinstance(v, int | float) and not isinstance(v, bool):
                (a["w"] if t["net_pnl"] > 0 else a["l"]).append(float(v))
    by_filter = {
        fid: {
            "field": a["field"],
            "operator": a["operator"],
            "threshold": a["threshold"],
            "trades": a["n"],
            "avg_observed_winners": round(statistics.fmean(a["w"]), 4) if a["w"] else None,
            "avg_observed_losers": round(statistics.fmean(a["l"]), 4) if a["l"] else None,
        }
        for fid, a in acc.items()
    }
    feats: dict[str, dict[str, list[float]]] = defaultdict(lambda: {"w": [], "l": []})
    for t in trades:
        for k, v in (t.get("features_at_entry") or {}).items():
            if isinstance(v, int | float) and not isinstance(v, bool) and not k.startswith("_"):
                feats[k]["w" if t["net_pnl"] > 0 else "l"].append(float(v))
    by_feature = {
        k: {
            "avg_winners": round(statistics.fmean(v["w"]), 4) if v["w"] else None,
            "avg_losers": round(statistics.fmean(v["l"]), 4) if v["l"] else None,
        }
        for k, v in sorted(feats.items())
        if k
        in ("rvol", "pct_change", "vwap_dist_pct", "spread_bps", "gap_pct", "volatility", "range_pct", "last")
    }
    return {"by_filter": by_filter, "by_feature_at_entry": by_feature}


def assumptions_list(cfg: BacktestConfig, provider: str, feed: str, adjustment: str) -> list[str]:
    c = cfg.costs
    a = [
        DISCLAIMER,
        f"Data: provider={provider or 'n/a'}, feed={feed or 'n/a'}, adjustment={adjustment}, 1-minute OHLCV bars only (no tick data).",
        "Regular session only (09:30-16:00 ET, 13:00 on early-close days); no premarket/postmarket, no current-day partial sessions.",
        "Signals are evaluated only at 1-minute bar close using information available at that time (no look-ahead).",
        f"Entry price model: {cfg.entry_price_model} "
        + (
            "(fill at the NEXT bar's open)."
            if cfg.entry_price_model == "next_open"
            else "(fill at the alert bar's close: optimistic, exact-close fills are not achievable)."
        ),
        f"Intrabar ambiguity policy: {cfg.intrabar_policy} (a 1-minute bar cannot show whether its high or low came first).",
        "Gap-through: if a bar opens beyond a stop/target, the fill is at the open, not the level.",
        f"Spread assumption: {c.spread_bps} bps full spread, half paid per market fill (limit target fills pay none unless limit_target_costs).",
        f"Slippage assumption: {f'{c.slippage_cents:.2f} cents' if c.slippage_cents is not None else f'{c.slippage_bps:.2f} bps'} adverse per market fill.",
        f"Commission: ${c.commission_per_share}/share, min ${c.min_commission_per_order}/order; fees {c.fee_bps} bps of notional per side.",
        f"Position sizing: {cfg.sizing.mode}; starting equity ${cfg.starting_equity:,.0f}; leverage {cfg.leverage}x buying power.",
        f"Limits: one entry/symbol/day={cfg.one_entry_per_symbol_per_day}; daily trade cap={cfg.max_trades_per_day}; max concurrent={cfg.max_concurrent_positions}; daily loss limit={cfg.daily_loss_limit} (blocks new entries; open positions are not force-closed).",
        f"Entry window (ET): {cfg.entry_start}-{cfg.entry_end}.",
        "Exit priority per bar: scheduled open exits (time/alert/multi-day) -> stop/trailing/target with the intrabar policy -> high-water-mark update. Same-day close is the fallback when hold_days=0.",
        "No partial fills, halts, auctions, short-borrow costs, dividends or queue position are modelled. Corporate actions rely on the provider's adjustment mode.",
        (
            f"Quotes: synthesised at each bar close with an assumed spread ({cfg.assumed_spread_bps} bps default, {len(cfg.assumed_spread_bps_by_symbol)} per-symbol overrides)."
            if cfg.assumed_spread_bps is not None or cfg.assumed_spread_bps_by_symbol
            else "Quotes: none (bars only); bid/ask/spread features are null and each filter's null_policy applies."
        ),
        "Universe: resolved as of each trading day; point-in-time and delisting handling apply only when a point-in-time universe is supplied.",
        "Results are historical simulations, not predictions or guarantees.",
    ]
    return a


def new_run_id() -> str:
    return "bt_" + uuid.uuid4().hex[:16]
