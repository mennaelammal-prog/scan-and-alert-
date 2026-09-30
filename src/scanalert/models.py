"""Internal, provider-neutral market-data model.

All datetimes are timezone-aware UTC. ``ts`` is the *source* (provider/exchange) timestamp and
``ingest_ts`` is when this process received the event, so latency and staleness are measurable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

Timeframe = Literal["1Min"]


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        raise ValueError("naive datetime")
    return ts.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_ts(value: str) -> datetime:
    """Parse ISO-8601 (incl. trailing Z and nanosecond fractions) into aware UTC."""
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    if "." in v:  # trim >6 fractional digits (Alpaca sends nanoseconds)
        head, _, rest = v.partition(".")
        frac = ""
        i = 0
        while i < len(rest) and rest[i].isdigit():
            frac += rest[i]
            i += 1
        tz = rest[i:]
        v = f"{head}.{frac[:6].ljust(6, '0')}{tz}"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class Trade:
    symbol: str
    ts: datetime
    price: float
    size: float
    trade_id: str = ""
    conditions: tuple[str, ...] = ()
    ingest_ts: datetime = field(default_factory=utcnow)
    kind: str = field(default="trade", init=False)

    @property
    def event_key(self) -> str:
        return f"t|{self.symbol}|{self.trade_id or iso(self.ts)}|{self.price}|{self.size}"


@dataclass(frozen=True, slots=True)
class Quote:
    """Top-of-book / NBBO quote."""

    symbol: str
    ts: datetime
    bid: float
    ask: float
    bid_size: float = 0.0
    ask_size: float = 0.0
    ingest_ts: datetime = field(default_factory=utcnow)
    kind: str = field(default="quote", init=False)

    @property
    def event_key(self) -> str:
        return f"q|{self.symbol}|{iso(self.ts)}|{self.bid}|{self.ask}|{self.bid_size}|{self.ask_size}"


@dataclass(frozen=True, slots=True)
class Bar:
    """One-minute bar. ``ts`` is the bar *start*. ``revision`` increases for updated/corrected bars."""

    symbol: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float | None = None
    trade_count: int | None = None
    timeframe: str = "1Min"
    revision: int = 0
    corrected: bool = False
    ingest_ts: datetime = field(default_factory=utcnow)
    kind: str = field(default="bar", init=False)

    @property
    def event_key(self) -> str:
        return f"b|{self.symbol}|{self.timeframe}|{iso(self.ts)}|r{self.revision}|{self.close}|{self.volume}"

    def validate(self) -> None:
        if not (self.low <= min(self.open, self.close) and self.high >= max(self.open, self.close)):
            raise ValueError(f"inconsistent OHLC for {self.symbol} @ {self.ts}")
        if self.volume < 0 or self.low <= 0:
            raise ValueError(f"invalid volume/price for {self.symbol} @ {self.ts}")


@dataclass(frozen=True, slots=True)
class SessionEvent:
    """Market session or per-symbol trading-status event (open/close/halt/resume)."""

    kind_: Literal["market_open", "market_close", "halt", "resume"]
    ts: datetime
    symbol: str | None = None
    detail: str = ""
    ingest_ts: datetime = field(default_factory=utcnow)
    kind: str = field(default="session", init=False)

    @property
    def event_key(self) -> str:
        return f"s|{self.kind_}|{self.symbol}|{iso(self.ts)}"


@dataclass(frozen=True, slots=True)
class ProviderEvent:
    """Provider diagnostics: connected/disconnected/reconnect/error/stale/gap/rate_limited/dropped."""

    kind_: Literal[
        "connected", "disconnected", "reconnecting", "error", "stale", "gap", "rate_limited", "backpressure"
    ]
    ts: datetime = field(default_factory=utcnow)
    detail: str = ""
    ingest_ts: datetime = field(default_factory=utcnow)
    kind: str = field(default="provider", init=False)

    @property
    def event_key(self) -> str:
        return f"p|{self.kind_}|{iso(self.ts)}|{self.detail}"


MarketEvent = Trade | Quote | Bar | SessionEvent | ProviderEvent


@dataclass(frozen=True, slots=True)
class HistoricalBarsRequest:
    symbols: list[str]
    start: datetime
    end: datetime
    timeframe: str = "1Min"
    adjustment: Literal["raw", "split", "all"] = "split"
    feed: str = "iex"


@dataclass(slots=True)
class ProviderHealth:
    provider: str
    connected: bool
    feed: str = ""
    last_event_ts: datetime | None = None
    last_ingest_ts: datetime | None = None
    reconnects: int = 0
    dropped_events: int = 0
    duplicate_events: int = 0
    rate_limited: int = 0
    gaps: int = 0
    late_events: int = 0
    corrections: int = 0
    stale: bool = False
    detail: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "connected": self.connected,
            "feed": self.feed,
            "last_event_ts": iso(self.last_event_ts),
            "last_ingest_ts": iso(self.last_ingest_ts),
            "reconnects": self.reconnects,
            "dropped_events": self.dropped_events,
            "duplicate_events": self.duplicate_events,
            "rate_limited": self.rate_limited,
            "gaps": self.gaps,
            "late_events": self.late_events,
            "corrections": self.corrections,
            "stale": self.stale,
            "detail": self.detail,
        }
