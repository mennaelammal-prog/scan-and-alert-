"""Per-symbol market state and the feature registry.

``SymbolState`` maintains incremental session accumulators (volume, VWAP, high/low, opening range,
rolling series) so the *same* feature code is used by live scanning and by historical replay.

Timing rules (no look-ahead):
* bar-derived features use only bars whose *start* is strictly before ``as_of`` minus one bar length
  when ``as_of`` is a bar close — callers pass ``as_of = bar.ts + 60s`` after ingesting that bar;
* ``last``/``bid``/``ask`` update per tick in live mode and per bar close in replay.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .calendar import ET, NyseCalendar, et_date
from .models import Bar, Quote, Trade

BASES = ("regular", "extended", "premarket", "postmarket")
MAX_LOOKBACK = 400
KEEP_DAYS = 25
SERIES_CAP = 2500


def basis_includes(basis: str, session: str) -> bool:
    if basis == "extended":
        return session in ("premarket", "regular", "postmarket")
    return basis == session


class Accum:
    """Running accumulators for one (day, basis)."""

    __slots__ = ("bars", "volume", "high", "low", "open", "pv", "dollar_volume", "idx", "cum")

    def __init__(self) -> None:
        self.bars: list[Bar] = []
        self.volume = 0.0
        self.high = -math.inf
        self.low = math.inf
        self.open: float | None = None
        self.pv = 0.0
        self.dollar_volume = 0.0
        self.idx: list[int] = []
        self.cum: list[float] = []

    def add(self, bar: Bar, minute_idx: int) -> None:
        if self.open is None:
            self.open = bar.open
        self.bars.append(bar)
        self.volume += bar.volume
        self.high = max(self.high, bar.high)
        self.low = min(self.low, bar.low)
        self.pv += (bar.high + bar.low + bar.close) / 3.0 * bar.volume
        self.dollar_volume += bar.close * bar.volume
        self.idx.append(minute_idx)
        self.cum.append(self.volume)

    @property
    def vwap(self) -> float | None:
        return self.pv / self.volume if self.volume > 0 else None

    @property
    def close(self) -> float | None:
        return self.bars[-1].close if self.bars else None

    def cum_volume_at(self, minute_idx: int) -> float | None:
        i = bisect_right(self.idx, minute_idx) - 1
        return self.cum[i] if i >= 0 else 0.0


@dataclass
class SymbolState:
    symbol: str
    calendar: NyseCalendar = field(default_factory=NyseCalendar)
    active: bool = True
    bars: dict[datetime, Bar] = field(default_factory=dict)
    last_trade_price: float | None = None
    last_trade_ts: datetime | None = None
    bid: float | None = None
    ask: float | None = None
    bid_size: float = 0.0
    ask_size: float = 0.0
    quote_ts: datetime | None = None
    halted: bool = False
    prev_close_override: float | None = None
    last_event_ts: datetime | None = None
    last_ingest_ts: datetime | None = None
    rvol_min_days: int = 3
    rvol_days: int = 20
    days: dict[date, dict[str, Accum]] = field(default_factory=dict)
    series: dict[str, list[Bar]] = field(default_factory=lambda: {b: [] for b in BASES})
    _last_bar_ts: datetime | None = None
    corrections_applied: int = 0
    late_bars: int = 0

    # ------------------------------------------------------------------ ingestion
    def on_quote(self, q: Quote) -> None:
        if q.bid <= 0 or q.ask <= 0 or q.ask < q.bid:
            return  # crossed/invalid quote: keep previous NBBO
        if self.quote_ts and q.ts < self.quote_ts:
            return
        self.bid, self.ask, self.bid_size, self.ask_size, self.quote_ts = (
            q.bid,
            q.ask,
            q.bid_size,
            q.ask_size,
            q.ts,
        )
        self._touch(q.ts, q.ingest_ts)

    def on_trade(self, t: Trade) -> None:
        if t.price <= 0:
            return
        if self.last_trade_ts and t.ts < self.last_trade_ts:
            return
        self.last_trade_price, self.last_trade_ts = t.price, t.ts
        self._touch(t.ts, t.ingest_ts)

    def _touch(self, ts: datetime, ingest: datetime) -> None:
        if self.last_event_ts is None or ts > self.last_event_ts:
            self.last_event_ts = ts
        if self.last_ingest_ts is None or ingest > self.last_ingest_ts:
            self.last_ingest_ts = ingest

    def on_bar(self, bar: Bar) -> bool:
        """Apply a bar. Returns True if state changed (False for duplicates / stale revisions)."""
        existing = self.bars.get(bar.ts)
        if existing is not None:
            if bar.revision < existing.revision:
                return False
            if (bar.open, bar.high, bar.low, bar.close, bar.volume) == (
                existing.open,
                existing.high,
                existing.low,
                existing.close,
                existing.volume,
            ) and bar.revision == existing.revision:
                return False
        self._touch(bar.ts + timedelta(minutes=1), bar.ingest_ts)
        self.bars[bar.ts] = bar
        if existing is None and (self._last_bar_ts is None or bar.ts > self._last_bar_ts):
            self._append(bar)
        else:
            if existing is not None:
                self.corrections_applied += 1
            else:
                self.late_bars += 1
            self._rebuild()
        return True

    def seed_history(self, bars: list[Bar]) -> None:
        for b in sorted(bars, key=lambda x: x.ts):
            self.on_bar(b)

    def _append(self, bar: Bar) -> None:
        d = et_date(bar.ts)
        session = self.calendar.session_at(bar.ts)
        idx = int(self.calendar.minutes_since_open(bar.ts))
        day = self.days.setdefault(d, {b: Accum() for b in BASES})
        for basis in BASES:
            if basis_includes(basis, session):
                day[basis].add(bar, idx)
                s = self.series[basis]
                s.append(bar)
                if len(s) > SERIES_CAP + 500:
                    del s[: len(s) - SERIES_CAP]
        self._last_bar_ts = bar.ts
        if len(self.days) > KEEP_DAYS:
            for old in sorted(self.days)[: len(self.days) - KEEP_DAYS]:
                del self.days[old]
            cutoff = min(self.days)
            for ts in [t for t in self.bars if et_date(t) < cutoff]:
                del self.bars[ts]

    def _rebuild(self) -> None:
        ordered = sorted(self.bars.values(), key=lambda b: b.ts)
        self.days.clear()
        self.series = {b: [] for b in BASES}
        self._last_bar_ts = None
        for b in ordered:
            self._append(b)

    # ------------------------------------------------------------------ queries
    @property
    def last_bar(self) -> Bar | None:
        return self.bars[self._last_bar_ts] if self._last_bar_ts else None

    def today(self, as_of: datetime) -> date:
        return et_date(as_of)

    def accum(self, as_of: datetime, basis: str) -> Accum | None:
        day = self.days.get(et_date(as_of))
        return day[basis] if day else None

    def prev_close(self, as_of: datetime) -> float | None:
        d = et_date(as_of)
        for pd in sorted((x for x in self.days if x < d), reverse=True):
            c = self.days[pd]["regular"].close
            if c is not None:
                return c
        return self.prev_close_override

    def last_price(self, as_of: datetime) -> float | None:
        """Most recent price known at ``as_of`` (tick if newer than last bar close, else bar close)."""
        bar = None
        a = self.accum(as_of, "extended")
        if a and a.bars:
            bar = a.bars[-1]
        bar_end = bar.ts + timedelta(minutes=1) if bar else None
        if (
            self.last_trade_price is not None
            and self.last_trade_ts is not None
            and self.last_trade_ts <= as_of
            and (bar_end is None or self.last_trade_ts >= bar_end)
        ):
            return self.last_trade_price
        return bar.close if bar else self.last_trade_price

    def rvol_baseline(self, as_of: datetime, idx: int) -> float | None:
        d = et_date(as_of)
        vals: list[float] = []
        for pd in sorted((x for x in self.days if x < d), reverse=True)[: self.rvol_days]:
            reg = self.days[pd]["regular"]
            if reg.bars:
                v = reg.cum_volume_at(idx)
                if v is not None:
                    vals.append(v)
        if len(vals) < self.rvol_min_days:
            return None
        return sum(vals) / len(vals)


# ============================================================================ feature registry
@dataclass(frozen=True)
class FeatureDef:
    name: str
    unit: str  # usd, pct, bps, shares, ratio, minutes, seconds, count, bool
    type: str  # number | bool
    fn: Callable[[FeatureContext, int | None], float | bool | None]
    description: str
    default_lookback: int | None = None  # None => feature takes no lookback
    max_lookback: int = MAX_LOOKBACK


class FeatureContext:
    """Lazy, cached feature evaluation for one (symbol, as_of, basis)."""

    def __init__(self, state: SymbolState, as_of: datetime, basis: str = "regular"):
        if basis not in BASES:
            raise ValueError(f"unknown session basis {basis!r}")
        self.state, self.as_of, self.basis = state, as_of, basis
        self._cache: dict[tuple[str, int | None], float | bool | None] = {}
        self.accum = state.accum(as_of, basis)
        # Series limited to bars that closed at or before as_of (no look-ahead).
        series = state.series[basis]
        end = as_of - timedelta(minutes=1)
        if series and series[-1].ts > end:
            k = len(series)
            while k > 0 and series[k - 1].ts > end:
                k -= 1
            series = series[:k]
        self.series = series
        self.notes: list[str] = []

    def get(self, name: str, lookback: int | None = None) -> float | bool | None:
        fd = FEATURES.get(name)
        if fd is None:
            raise KeyError(f"unknown feature {name!r}")
        if fd.default_lookback is None:
            lookback = None
        else:
            lookback = fd.default_lookback if lookback is None else lookback
            if not 1 <= lookback <= fd.max_lookback:
                raise ValueError(f"lookback for {name} must be 1..{fd.max_lookback}")
        key = (name, lookback)
        if key not in self._cache:
            self._cache[key] = fd.fn(self, lookback)
        return self._cache[key]

    # helpers
    def closes(self, n: int) -> list[float]:
        return [b.close for b in self.series[-n:]]

    def last(self) -> float | None:
        return self.state.last_price(self.as_of)

    def last_is_bar_close(self) -> bool:
        """True when the last price is the close of the newest bar (no newer tick)."""
        st, a = self.state, self.state.accum(self.as_of, "extended")
        if not a or not a.bars:
            return False
        newest = a.bars[-1]
        return st.last_trade_ts is None or st.last_trade_ts < newest.ts + timedelta(minutes=1)

    def prior(self, seq: list[Bar]) -> list[Bar]:
        """Bars strictly before the one that produced ``last`` (a close cannot 'break' its own high)."""
        return seq[:-1] if seq and self.last_is_bar_close() else seq


def _num(x: float | None) -> float | None:
    return None if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))) else x


def _f_last(c: FeatureContext, n: int | None) -> float | None:
    return c.last()


def _f_bid(c: FeatureContext, n: int | None) -> float | None:
    return c.state.bid


def _f_ask(c: FeatureContext, n: int | None) -> float | None:
    return c.state.ask


def _f_spread(c: FeatureContext, n: int | None) -> float | None:
    s = c.state
    return s.ask - s.bid if s.bid is not None and s.ask is not None else None


def _f_spread_pct(c: FeatureContext, n: int | None) -> float | None:
    s = c.state
    if s.bid is None or s.ask is None:
        return None
    mid = (s.bid + s.ask) / 2
    return (s.ask - s.bid) / mid * 100 if mid > 0 else None


def _f_spread_bps(c: FeatureContext, n: int | None) -> float | None:
    v = _f_spread_pct(c, n)
    return v * 100 if v is not None else None


def _f_volume(c: FeatureContext, n: int | None) -> float | None:
    return c.accum.volume if c.accum and c.accum.bars else None


def _f_rvol(c: FeatureContext, n: int | None) -> float | None:
    if c.basis != "regular" or not c.accum or not c.accum.bars:
        return None
    idx = c.accum.idx[-1]
    base = c.state.rvol_baseline(c.as_of, idx)
    if not base:
        return None
    return c.accum.volume / base


def _f_pct_change(c: FeatureContext, n: int | None) -> float | None:
    last, pc = c.last(), c.state.prev_close(c.as_of)
    return (last - pc) / pc * 100 if last is not None and pc else None


def _f_dollar_change(c: FeatureContext, n: int | None) -> float | None:
    last, pc = c.last(), c.state.prev_close(c.as_of)
    return last - pc if last is not None and pc else None


def _f_prev_close(c: FeatureContext, n: int | None) -> float | None:
    return c.state.prev_close(c.as_of)


def _f_gap_pct(c: FeatureContext, n: int | None) -> float | None:
    pc = c.state.prev_close(c.as_of)
    o = c.accum.open if c.accum else None
    return (o - pc) / pc * 100 if o is not None and pc else None


def _f_day_open(c: FeatureContext, n: int | None) -> float | None:
    return c.accum.open if c.accum else None


def _f_day_high(c: FeatureContext, n: int | None) -> float | None:
    return c.accum.high if c.accum and c.accum.bars else None


def _f_day_low(c: FeatureContext, n: int | None) -> float | None:
    return c.accum.low if c.accum and c.accum.bars else None


def _f_range(c: FeatureContext, n: int | None) -> float | None:
    if not c.accum or not c.accum.bars:
        return None
    return c.accum.high - c.accum.low


def _f_range_pct(c: FeatureContext, n: int | None) -> float | None:
    r, last = _f_range(c, n), c.last()
    return r / last * 100 if r is not None and last else None


def _f_range_position(c: FeatureContext, n: int | None) -> float | None:
    r, last = _f_range(c, n), c.last()
    if r is None or last is None or r <= 0 or not c.accum:
        return None
    return (last - c.accum.low) / r


def _f_vwap(c: FeatureContext, n: int | None) -> float | None:
    return c.accum.vwap if c.accum else None


def _f_vwap_dist_pct(c: FeatureContext, n: int | None) -> float | None:
    v, last = _f_vwap(c, n), c.last()
    return (last - v) / v * 100 if v and last is not None else None


def _orb(c: FeatureContext, n: int) -> tuple[float, float] | None:
    """Opening range of the first ``n`` regular minutes; defined only once that window has completed."""
    reg = c.state.accum(c.as_of, "regular")
    if not reg or not reg.bars or reg.idx[-1] < n - 1:
        return None
    window = [b for b, i in zip(reg.bars, reg.idx, strict=True) if i < n]
    if not window:
        return None
    return max(b.high for b in window), min(b.low for b in window)


def _f_orb_high(c: FeatureContext, n: int | None) -> float | None:
    r = _orb(c, n or 15)
    return r[0] if r else None


def _f_orb_low(c: FeatureContext, n: int | None) -> float | None:
    r = _orb(c, n or 15)
    return r[1] if r else None


def _f_orb_up(c: FeatureContext, n: int | None) -> bool | None:
    r, last = _orb(c, n or 15), c.last()
    if r is None or last is None:
        return None
    return last > r[0]


def _f_orb_down(c: FeatureContext, n: int | None) -> bool | None:
    r, last = _orb(c, n or 15), c.last()
    if r is None or last is None:
        return None
    return last < r[1]


def _f_new_high(c: FeatureContext, n: int | None) -> bool | None:
    """Last price exceeds the highest high of the previous ``n`` completed bars (excluding the bar that set ``last``)."""
    k = n or 30
    prior = c.prior(c.series)[-k:]
    last = c.last()
    if len(prior) < k or last is None:
        return None
    return last > max(b.high for b in prior)


def _f_new_low(c: FeatureContext, n: int | None) -> bool | None:
    k = n or 30
    prior = c.prior(c.series)[-k:]
    last = c.last()
    if len(prior) < k or last is None:
        return None
    return last < min(b.low for b in prior)


def _f_new_day_high(c: FeatureContext, n: int | None) -> bool | None:
    if not c.accum:
        return None
    prior = c.prior(c.accum.bars)
    last = c.last()
    if not prior or last is None:
        return None
    return last > max(b.high for b in prior)


def _f_new_day_low(c: FeatureContext, n: int | None) -> bool | None:
    if not c.accum:
        return None
    prior = c.prior(c.accum.bars)
    last = c.last()
    if not prior or last is None:
        return None
    return last < min(b.low for b in prior)


def _f_sma(c: FeatureContext, n: int | None) -> float | None:
    assert n is not None
    cl = c.closes(n)
    return sum(cl) / n if len(cl) == n else None


def _f_ema(c: FeatureContext, n: int | None) -> float | None:
    assert n is not None
    cl = c.closes(max(n * 3, n))
    if len(cl) < n:
        return None
    k = 2 / (n + 1)
    ema = sum(cl[:n]) / n
    for x in cl[n:]:
        ema = x * k + ema * (1 - k)
    return ema


def _f_ma_dist_pct(c: FeatureContext, n: int | None) -> float | None:
    m, last = _f_sma(c, n), c.last()
    return (last - m) / m * 100 if m and last is not None else None


def _f_volatility(c: FeatureContext, n: int | None) -> float | None:
    """Standard deviation of 1-minute log returns over the last ``n`` bars, in percent."""
    assert n is not None
    cl = c.closes(n + 1)
    if len(cl) < n + 1:
        return None
    rets = [math.log(b / a) for a, b in zip(cl, cl[1:], strict=False) if a > 0 and b > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * 100


def _f_atr(c: FeatureContext, n: int | None) -> float | None:
    assert n is not None
    s = c.series[-(n + 1) :]
    if len(s) < n + 1:
        return None
    trs = [
        max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close))
        for a, b in zip(s, s[1:], strict=False)
    ]
    return sum(trs) / len(trs)


def _f_atr_pct(c: FeatureContext, n: int | None) -> float | None:
    a, last = _f_atr(c, n), c.last()
    return a / last * 100 if a is not None and last else None


def _f_ret(c: FeatureContext, n: int | None) -> float | None:
    assert n is not None
    s = c.series
    if len(s) < n + 1:
        return None
    a = s[-(n + 1)].close
    last = c.last()
    return (last - a) / a * 100 if a and last is not None else None


def _f_highest(c: FeatureContext, n: int | None) -> float | None:
    assert n is not None
    s = c.series[-n:]
    return max(b.high for b in s) if len(s) == n else None


def _f_lowest(c: FeatureContext, n: int | None) -> float | None:
    assert n is not None
    s = c.series[-n:]
    return min(b.low for b in s) if len(s) == n else None


def _f_avg_volume(c: FeatureContext, n: int | None) -> float | None:
    assert n is not None
    s = c.series[-n:]
    return sum(b.volume for b in s) / n if len(s) == n else None


def _f_vol_sum(c: FeatureContext, n: int | None) -> float | None:
    assert n is not None
    s = c.series[-n:]
    return sum(b.volume for b in s) if len(s) == n else None


def _f_dollar_volume(c: FeatureContext, n: int | None) -> float | None:
    return c.accum.dollar_volume if c.accum and c.accum.bars else None


def _f_avg_dollar_volume(c: FeatureContext, n: int | None) -> float | None:
    assert n is not None
    s = c.series[-n:]
    return sum(b.close * b.volume for b in s) / n if len(s) == n else None


def _f_bars_today(c: FeatureContext, n: int | None) -> float | None:
    return float(len(c.accum.bars)) if c.accum else 0.0


def _f_minutes_since_open(c: FeatureContext, n: int | None) -> float | None:
    return c.state.calendar.minutes_since_open(c.as_of)


def _f_time_of_day(c: FeatureContext, n: int | None) -> float | None:
    t = c.as_of.astimezone(ET)
    return t.hour * 60 + t.minute + t.second / 60


def _f_minutes_to_close(c: FeatureContext, n: int | None) -> float | None:
    cal = c.state.calendar
    d = et_date(c.as_of)
    if not cal.is_trading_day(d):
        return None
    return (cal.close_dt(d) - c.as_of).total_seconds() / 60


def _f_data_age(c: FeatureContext, n: int | None) -> float | None:
    s = c.state
    if s.last_event_ts is None:
        return None
    return max(0.0, (c.as_of - s.last_event_ts).total_seconds())


def _f_halted(c: FeatureContext, n: int | None) -> bool | None:
    return c.state.halted


def _f_bid_size(c: FeatureContext, n: int | None) -> float | None:
    return c.state.bid_size if c.state.bid is not None else None


def _f_ask_size(c: FeatureContext, n: int | None) -> float | None:
    return c.state.ask_size if c.state.ask is not None else None


def _reg(*defs: FeatureDef) -> dict[str, FeatureDef]:
    return {d.name: d for d in defs}


FEATURES: dict[str, FeatureDef] = _reg(
    FeatureDef("last", "usd", "number", _f_last, "Last price (latest tick, else last bar close)"),
    FeatureDef("bid", "usd", "number", _f_bid, "Best bid (NBBO)"),
    FeatureDef("ask", "usd", "number", _f_ask, "Best ask (NBBO)"),
    FeatureDef("bid_size", "shares", "number", _f_bid_size, "Best bid size"),
    FeatureDef("ask_size", "shares", "number", _f_ask_size, "Best ask size"),
    FeatureDef("spread", "usd", "number", _f_spread, "Ask minus bid"),
    FeatureDef("spread_pct", "pct", "number", _f_spread_pct, "Spread as % of mid"),
    FeatureDef("spread_bps", "bps", "number", _f_spread_bps, "Spread in basis points of mid"),
    FeatureDef("volume", "shares", "number", _f_volume, "Cumulative volume for the session basis"),
    FeatureDef(
        "rvol",
        "ratio",
        "number",
        _f_rvol,
        "Cumulative volume / avg cumulative volume at same minute (prior days)",
    ),
    FeatureDef("prev_close", "usd", "number", _f_prev_close, "Previous regular-session close"),
    FeatureDef("pct_change", "pct", "number", _f_pct_change, "% change vs previous close"),
    FeatureDef("dollar_change", "usd", "number", _f_dollar_change, "$ change vs previous close"),
    FeatureDef("gap_pct", "pct", "number", _f_gap_pct, "Session open vs previous close, %"),
    FeatureDef("day_open", "usd", "number", _f_day_open, "Session open"),
    FeatureDef("day_high", "usd", "number", _f_day_high, "Session high"),
    FeatureDef("day_low", "usd", "number", _f_day_low, "Session low"),
    FeatureDef("range", "usd", "number", _f_range, "Session high minus low"),
    FeatureDef("range_pct", "pct", "number", _f_range_pct, "Session range as % of last"),
    FeatureDef(
        "range_position", "ratio", "number", _f_range_position, "Position in session range, 0=low 1=high"
    ),
    FeatureDef("vwap", "usd", "number", _f_vwap, "Session VWAP (typical price x volume)"),
    FeatureDef("vwap_dist_pct", "pct", "number", _f_vwap_dist_pct, "Distance from VWAP, %"),
    FeatureDef(
        "orb_high",
        "usd",
        "number",
        _f_orb_high,
        "Opening-range high (lookback = minutes, default 15)",
        15,
        120,
    ),
    FeatureDef(
        "orb_low", "usd", "number", _f_orb_low, "Opening-range low (lookback = minutes, default 15)", 15, 120
    ),
    FeatureDef("orb_breakout_up", "bool", "bool", _f_orb_up, "Last above opening-range high", 15, 120),
    FeatureDef("orb_breakout_down", "bool", "bool", _f_orb_down, "Last below opening-range low", 15, 120),
    FeatureDef("new_high", "bool", "bool", _f_new_high, "Last above highest high of previous n bars", 30),
    FeatureDef("new_low", "bool", "bool", _f_new_low, "Last below lowest low of previous n bars", 30),
    FeatureDef("new_day_high", "bool", "bool", _f_new_day_high, "Last above all earlier session highs"),
    FeatureDef("new_day_low", "bool", "bool", _f_new_day_low, "Last below all earlier session lows"),
    FeatureDef("sma", "usd", "number", _f_sma, "Simple moving average of closes", 20),
    FeatureDef("ema", "usd", "number", _f_ema, "Exponential moving average of closes", 20),
    FeatureDef("ma_dist_pct", "pct", "number", _f_ma_dist_pct, "Distance of last from SMA, %", 20),
    FeatureDef("volatility", "pct", "number", _f_volatility, "Stdev of 1-min log returns, %", 20),
    FeatureDef("atr", "usd", "number", _f_atr, "Average true range (1-min bars)", 14),
    FeatureDef("atr_pct", "pct", "number", _f_atr_pct, "ATR as % of last", 14),
    FeatureDef("ret", "pct", "number", _f_ret, "% return over n bars", 5),
    FeatureDef("highest", "usd", "number", _f_highest, "Highest high over n bars", 30),
    FeatureDef("lowest", "usd", "number", _f_lowest, "Lowest low over n bars", 30),
    FeatureDef("avg_volume", "shares", "number", _f_avg_volume, "Average per-bar volume over n bars", 20),
    FeatureDef("vol_sum", "shares", "number", _f_vol_sum, "Sum of volume over n bars", 5),
    FeatureDef("dollar_volume", "usd", "number", _f_dollar_volume, "Session dollar volume (close x volume)"),
    FeatureDef(
        "avg_dollar_volume", "usd", "number", _f_avg_dollar_volume, "Average per-bar dollar volume", 20
    ),
    FeatureDef("bars_today", "count", "number", _f_bars_today, "Number of bars in the session basis"),
    FeatureDef("minutes_since_open", "minutes", "number", _f_minutes_since_open, "Minutes since 09:30 ET"),
    FeatureDef("time_of_day", "minutes", "number", _f_time_of_day, "Minutes since midnight ET"),
    FeatureDef(
        "minutes_to_close", "minutes", "number", _f_minutes_to_close, "Minutes until the session close"
    ),
    FeatureDef(
        "data_age_seconds", "seconds", "number", _f_data_age, "Age of the newest event for this symbol"
    ),
    FeatureDef("halted", "bool", "bool", _f_halted, "Symbol currently halted"),
)


def compute_all(
    state: SymbolState, as_of: datetime, basis: str = "regular"
) -> dict[str, float | bool | None]:
    """Every feature at its default lookback; used for the ``feature_snapshot`` of alerts."""
    ctx = FeatureContext(state, as_of, basis)
    out: dict[str, float | bool | None] = {}
    for name in FEATURES:
        v = ctx.get(name)
        if isinstance(v, float):
            v = _num(round(v, 6))
        out[name] = v
    return out
