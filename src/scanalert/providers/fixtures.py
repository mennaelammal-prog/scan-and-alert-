"""Deterministic synthetic market data and a replay provider (no credentials, no network).

The generated data is *synthetic*: it exists to make scanner/alert/backtest behaviour reproducible.
Scenario symbols exercise specific features (breakout, gap, wide spread, illiquid, ...).
"""

from __future__ import annotations

import asyncio
import csv
import math
import random
import zlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from ..calendar import NyseCalendar
from ..models import (
    Bar,
    HistoricalBarsRequest,
    MarketEvent,
    ProviderEvent,
    ProviderHealth,
    Quote,
    SessionEvent,
    Trade,
    iso,
    parse_ts,
    utcnow,
)

DEFAULT_START = date(2026, 8, 31)
DEFAULT_DAYS = 20


@dataclass(frozen=True)
class Scenario:
    base_price: float
    daily_vol_pct: float  # per-minute return stdev in percent
    drift_bps_per_min: float
    base_minute_volume: float
    spread_bps: float


SCENARIOS: dict[str, Scenario] = {
    "ORBX": Scenario(25.0, 0.06, 0.0, 9_000, 6.0),  # opening-range breakouts
    "GAPU": Scenario(40.0, 0.09, 0.0, 14_000, 5.0),  # gap-up momentum
    "TRND": Scenario(60.0, 0.05, 0.25, 12_000, 4.0),  # steady uptrend
    "FLAT": Scenario(80.0, 0.02, 0.0, 6_000, 4.0),  # quiet
    "WIDE": Scenario(15.0, 0.07, 0.0, 8_000, 150.0),  # breakouts but very wide spread
    "SPKE": Scenario(30.0, 0.05, 0.0, 7_000, 8.0),  # intraday volume spike
    "DRFT": Scenario(50.0, 0.05, -0.25, 9_000, 5.0),  # downtrend
    "LOWV": Scenario(1.2, 0.20, 0.0, 300, 90.0),  # sub-$2, illiquid
}


@dataclass
class FixtureData:
    bars: dict[str, list[Bar]]
    spread_bps: dict[str, float]
    days: list[date]
    seed: int

    def symbols(self) -> list[str]:
        return sorted(self.bars)

    def day_bars(self, symbol: str, d: date) -> list[Bar]:
        from ..calendar import et_date

        return [b for b in self.bars[symbol] if et_date(b.ts) == d]


def _u_shape(i: int, n: int) -> float:
    x = i / max(1, n - 1)
    return (
        0.55 + 2.2 * (x - 0.5) ** 2 * 2 + 1.6 * math.exp(-x * 14)
    )  # heavy open, lighter midday, lift at close


def _rng(seed: int, symbol: str, d: date | None = None) -> random.Random:
    key = f"{seed}|{symbol}|{d.isoformat() if d else ''}".encode()
    return random.Random(zlib.crc32(key))


def _round(x: float) -> float:
    return round(x, 2) if x >= 1 else round(x, 4)


def generate_fixture(
    symbols: list[str] | None = None,
    start: date = DEFAULT_START,
    n_days: int = DEFAULT_DAYS,
    seed: int = 42,
    calendar: NyseCalendar | None = None,
) -> FixtureData:
    cal = calendar or NyseCalendar()
    symbols = symbols or list(SCENARIOS)
    days: list[date] = []
    d = start
    while len(days) < n_days:
        if cal.is_trading_day(d):
            days.append(d)
        d += timedelta(days=1)
    bars: dict[str, list[Bar]] = {}
    spreads: dict[str, float] = {}
    for sym in symbols:
        sc = SCENARIOS.get(sym, Scenario(20.0 + (zlib.crc32(sym.encode()) % 80), 0.06, 0.0, 8_000, 8.0))
        spreads[sym] = sc.spread_bps
        out: list[Bar] = []
        prev_close = sc.base_price
        for di, day in enumerate(days):
            r = _rng(seed, sym, day)
            minutes = cal.regular_minutes(day)
            n = len(minutes)
            gap = r.gauss(0, 0.002)
            is_last = di == len(days) - 1
            breakout_day = sym in ("ORBX", "WIDE") and (is_last or r.random() < 0.3)
            gap_day = sym == "GAPU" and (is_last or r.random() < 0.3)
            if gap_day:
                gap = 0.05 + r.random() * 0.02
            # Overnight mean reversion (trend symbols excepted) is folded into the opening gap so that the
            # observable gap versus the true previous close is exactly what a scanner would see.
            reverted = prev_close if sym in ("TRND", "DRFT") else prev_close * 0.5 + sc.base_price * 0.5
            price = (prev_close if gap_day else reverted) * (1 + gap)
            breakout_min = r.randint(25, 60)
            breakout_dir = 1 if r.random() < 0.65 else -1
            if is_last and sym in ("ORBX", "WIDE"):
                breakout_min, breakout_dir = 40, 1
            spike_min = r.randint(60, 200) if sym == "SPKE" else -1
            for i, ts in enumerate(minutes):
                sigma = sc.daily_vol_pct / 100
                drift = sc.drift_bps_per_min / 10_000
                vol_mult = 1.0
                if sym in ("ORBX", "WIDE"):
                    if breakout_day and i >= 5:
                        vol_mult = 3.0  # sustained interest on breakout days => elevated relative volume
                    if i < 15:
                        sigma *= 0.35  # tight opening range
                    elif breakout_day and breakout_min <= i < breakout_min + 12:
                        drift += breakout_dir * 0.0012
                        vol_mult = 6.0
                        sigma *= 1.2
                    elif breakout_day and i >= breakout_min + 12:
                        drift += breakout_dir * 0.00015
                if gap_day:
                    if i < 5:
                        vol_mult = 3.0
                    drift += 0.00045 if i < 120 else 0.00012
                    vol_mult = max(vol_mult, 2.6 if i < 120 else 1.8)
                if sym == "SPKE" and spike_min <= i < spike_min + 3:
                    vol_mult = 12.0
                    drift += 0.001
                elif sym == "SPKE" and spike_min + 3 <= i < spike_min + 30:
                    drift -= 0.0002
                o = price
                c = o * math.exp(r.gauss(drift, sigma))
                wick = abs(r.gauss(0, sigma * 0.6))
                h = max(o, c) * (1 + wick)
                lo = min(o, c) * (1 - abs(r.gauss(0, sigma * 0.6)))
                o, c, h, lo = _round(o), _round(c), _round(h), _round(lo)
                h, lo = max(h, o, c), min(lo, o, c)
                if lo <= 0:
                    lo = min(o, c)
                vol = max(1.0, sc.base_minute_volume * _u_shape(i, n) * vol_mult * math.exp(r.gauss(0, 0.35)))
                vol = float(int(vol))
                vw = _round((h + lo + c) / 3)
                out.append(Bar(sym, ts, o, h, lo, c, vol, vwap=vw, trade_count=max(1, int(vol / 90))))
                price = c
            prev_close = price
        bars[sym] = out
    return FixtureData(bars=bars, spread_bps=spreads, days=days, seed=seed)


CSV_FIELDS = ["symbol", "ts", "open", "high", "low", "close", "volume", "vwap"]


def write_csv(data: FixtureData, path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_FIELDS)
        for sym in data.symbols():
            for b in data.bars[sym]:
                w.writerow([sym, iso(b.ts), b.open, b.high, b.low, b.close, int(b.volume), b.vwap])
                n += 1
    meta = path.with_suffix(".meta.txt")
    meta.write_text(
        f"seed={data.seed}\ndays={','.join(d.isoformat() for d in data.days)}\n"
        f"spread_bps={','.join(f'{k}:{v}' for k, v in sorted(data.spread_bps.items()))}\n"
    )
    return n


def read_csv(path: Path) -> FixtureData:
    from ..calendar import et_date

    bars: dict[str, list[Bar]] = {}
    with path.open() as fh:
        for row in csv.DictReader(fh):
            b = Bar(
                row["symbol"],
                parse_ts(row["ts"]),
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                float(row["volume"]),
                vwap=float(row["vwap"]) if row.get("vwap") else None,
            )
            b.validate()
            bars.setdefault(b.symbol, []).append(b)
    meta = {}
    mp = path.with_suffix(".meta.txt")
    if mp.exists():
        for line in mp.read_text().splitlines():
            k, _, v = line.partition("=")
            meta[k] = v
    spreads = {k: float(v) for k, v in (kv.split(":") for kv in meta.get("spread_bps", "").split(",") if kv)}
    days = sorted({et_date(b.ts) for bs in bars.values() for b in bs})
    return FixtureData(bars, {s: spreads.get(s, 8.0) for s in bars}, days, int(meta.get("seed", 0) or 0))


def synth_quote(symbol: str, ts: datetime, price: float, spread_bps: float) -> Quote:
    half = max(0.005, price * spread_bps / 20_000)
    bid = _round(price - half)
    ask = _round(price + half)
    if ask <= bid:
        ask = _round(bid + 0.01)
    return Quote(symbol, ts, bid, ask, 300.0, 300.0)


@dataclass
class FaultPlan:
    """Failure injection for tests: applies to the *replay* day event stream."""

    duplicate_every: int = 0  # re-emit every Nth bar event
    correct_bar_at: int = -1  # after this bar index emit a corrected revision
    late_bar_at: int = -1  # hold this bar back and emit it 3 bars later
    disconnect_after: int = -1  # raise ConnectionError once after N events
    halt_at: int = -1  # emit a halt event for the first symbol at bar index
    resume_at: int = -1
    _disconnected: bool = field(default=False, repr=False)


class FixtureProvider:
    """Replays deterministic fixture data as a live-style stream (Bar + Quote + Trade + session events)."""

    name = "fixture"

    def __init__(
        self,
        data: FixtureData,
        calendar: NyseCalendar | None = None,
        replay_days: int = 1,
        speed: float = 0.0,
        faults: FaultPlan | None = None,
    ):
        self.data = data
        self.calendar = calendar or NyseCalendar()
        self.replay_days = replay_days
        self.speed = speed
        self.faults = faults or FaultPlan()
        self.subs_bars: set[str] = set()
        self.subs_quotes: set[str] = set()
        self.subs_trades: set[str] = set()
        self._health = ProviderHealth(provider="fixture", connected=False, feed="synthetic")
        self._stop = asyncio.Event()
        self._pos = 0
        self.sim_now: datetime | None = None
        self._script: list[MarketEvent] | None = None

    # ------------------------------------------------------------- provider API
    async def subscribe_trades(self, symbols: list[str]) -> None:
        self.subs_trades |= set(symbols)

    async def subscribe_quotes(self, symbols: list[str]) -> None:
        self.subs_quotes |= set(symbols)

    async def subscribe_bars(self, symbols: list[str], timeframe: str = "1Min") -> None:
        if timeframe != "1Min":
            raise ValueError("fixture provider supports 1Min bars only")
        self.subs_bars |= set(symbols)

    async def historical_bars(self, request: HistoricalBarsRequest) -> list[Bar]:
        out: list[Bar] = []
        for s in request.symbols:
            out.extend(b for b in self.data.bars.get(s, []) if request.start <= b.ts < request.end)
        return sorted(out, key=lambda b: (b.ts, b.symbol))

    async def health(self) -> ProviderHealth:
        return self._health

    async def start(self) -> None:
        self._stop.clear()
        self._health.connected = True

    async def stop(self) -> None:
        self._stop.set()
        self._health.connected = False

    # ------------------------------------------------------------------ replay
    @property
    def live_days(self) -> list[date]:
        return self.data.days[-self.replay_days :]

    @property
    def history_days(self) -> list[date]:
        return self.data.days[: -self.replay_days]

    def history_end(self) -> datetime:
        return self.calendar.open_dt(self.live_days[0])

    def _build_script(self) -> list[MarketEvent]:
        syms = sorted(self.subs_bars | self.subs_quotes | self.subs_trades or set(self.data.bars))
        events: list[MarketEvent] = []
        idx = 0
        for day in self.live_days:
            events.append(SessionEvent("market_open", self.calendar.open_dt(day), detail=day.isoformat()))
            minutes = self.calendar.regular_minutes(day)
            by_sym = {s: {b.ts: b for b in self.data.day_bars(s, day)} for s in syms if s in self.data.bars}
            held: list[Bar] = []
            for m in minutes:
                for s in sorted(by_sym):
                    b = by_sym[s].get(m)
                    if b is None:
                        continue
                    ing = b.ts + timedelta(minutes=1)
                    sp = self.data.spread_bps.get(s, 8.0)
                    events.append(synth_quote(s, b.ts + timedelta(seconds=1), b.open, sp))
                    events.append(synth_quote(s, b.ts + timedelta(seconds=59), b.close, sp))
                    events.append(
                        Trade(
                            s,
                            b.ts + timedelta(seconds=59, milliseconds=500),
                            b.close,
                            100.0,
                            trade_id=f"{s}-{int(b.ts.timestamp())}",
                        )
                    )
                    bar = Bar(
                        s,
                        b.ts,
                        b.open,
                        b.high,
                        b.low,
                        b.close,
                        b.volume,
                        b.vwap,
                        b.trade_count,
                        ingest_ts=ing,
                    )
                    f = self.faults
                    if f.late_bar_at == idx and s == syms[0]:
                        held.append(bar)
                    else:
                        events.append(bar)
                    if f.duplicate_every and idx % f.duplicate_every == 0:
                        events.append(bar)
                    if f.correct_bar_at == idx and s == syms[0]:
                        events.append(
                            Bar(
                                s,
                                b.ts,
                                b.open,
                                b.high,
                                b.low,
                                b.close + 0.01,
                                b.volume,
                                b.vwap,
                                b.trade_count,
                                revision=1,
                                corrected=True,
                            )
                        )
                    if held and idx == f.late_bar_at + 3:
                        events.extend(held)
                        held = []
                idx += 1
                if self.faults.halt_at == idx and syms:
                    events.append(
                        SessionEvent(
                            "halt",
                            m + timedelta(minutes=1),
                            symbol=syms[0],
                            detail="volatility halt (fixture)",
                        )
                    )
                if self.faults.resume_at == idx and syms:
                    events.append(
                        SessionEvent(
                            "resume", m + timedelta(minutes=1), symbol=syms[0], detail="resume (fixture)"
                        )
                    )
            events.extend(held)
            events.append(SessionEvent("market_close", self.calendar.close_dt(day), detail=day.isoformat()))
        return events

    async def events(self) -> AsyncIterator[MarketEvent]:
        if self._script is None:
            self._script = self._build_script()
        script = self._script
        current_minute: datetime | None = None
        while self._pos < len(script) and not self._stop.is_set():
            ev = script[self._pos]
            f = self.faults
            if f.disconnect_after >= 0 and not f._disconnected and self._pos >= f.disconnect_after:
                f._disconnected = True
                self._health.reconnects += 1
                yield ProviderEvent("disconnected", detail="fixture disconnect (fault injection)")
                yield ProviderEvent("reconnecting", detail="fixture resume")
                yield ProviderEvent("connected", detail="subscriptions restored")
                continue
            ts = getattr(ev, "ts", None)
            if ts is not None and not isinstance(ev, ProviderEvent):
                minute = ts.replace(second=0, microsecond=0)
                if isinstance(ev, Bar):
                    minute = ev.ts
                # Pace at minute boundaries so every symbol of one minute is delivered together.
                if self.speed > 0 and current_minute is not None and minute > current_minute:
                    await asyncio.sleep(60.0 / self.speed)
                if current_minute is None or minute > current_minute:
                    current_minute = minute
            self._pos += 1
            if ts is not None and not isinstance(ev, ProviderEvent):
                cand = ts + timedelta(minutes=1) if isinstance(ev, Bar) else ts
                if self.sim_now is None or cand > self.sim_now:  # simulated time never runs backwards
                    self.sim_now = cand
                self._health.last_event_ts = ts
                self._health.last_ingest_ts = utcnow()
            yield ev
            if self._pos % 200 == 0:
                await asyncio.sleep(0)
        self._health.connected = False
