"""Resilient streaming core shared by real adapters: reconnect, resubscribe, stale detection,
backpressure, duplicate protection, rate-limit backoff and data-gap diagnostics.

Nothing here logs message bodies: auth frames contain secrets.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Protocol

from ..calendar import et_date
from ..models import Bar, MarketEvent, ProviderEvent, ProviderHealth, Quote, SessionEvent, Trade, utcnow

log = logging.getLogger("scanalert.stream")


class Connection(Protocol):
    async def send(self, message: Any) -> None: ...
    async def recv(self) -> Any: ...
    async def close(self) -> None: ...


class RateLimited(Exception):
    def __init__(self, retry_after: float = 30.0, detail: str = "rate limited"):
        super().__init__(detail)
        self.retry_after = retry_after


class AuthError(Exception):
    """Credentials rejected: not retried indefinitely."""


class StreamStale(Exception):
    pass


@dataclass
class Backoff:
    base: float = 1.0
    cap: float = 60.0
    jitter: float = 0.3  # +/- fraction
    rng: random.Random = field(default_factory=random.Random)
    attempt: int = 0

    def next(self) -> float:
        d = min(self.cap, self.base * (2**self.attempt))
        self.attempt += 1
        if self.jitter:
            d *= 1 + self.rng.uniform(-self.jitter, self.jitter)
        return max(0.0, d)

    def reset(self) -> None:
        self.attempt = 0


class EventBuffer:
    """Two-lane bounded buffer preserving arrival order.

    Bars/session/provider events are never dropped; when the tick lane is full the *oldest tick* is
    dropped (and counted). Each event carries a sequence number so ``get`` merges both lanes in order.
    """

    def __init__(self, tick_capacity: int = 10_000, critical_capacity: int = 200_000):
        self.ticks: deque[tuple[int, MarketEvent]] = deque()
        self.critical: deque[tuple[int, MarketEvent]] = deque()
        self.tick_capacity, self.critical_capacity = tick_capacity, critical_capacity
        self.dropped_ticks = 0
        self._seq = 0
        self._cond = asyncio.Event()

    def put(self, ev: MarketEvent) -> bool:
        """Returns False if an event had to be dropped (backpressure)."""
        ok = True
        self._seq += 1
        if isinstance(ev, Trade | Quote):
            if len(self.ticks) >= self.tick_capacity:
                self.ticks.popleft()
                self.dropped_ticks += 1
                ok = False
            self.ticks.append((self._seq, ev))
        else:
            if len(self.critical) >= self.critical_capacity:
                self.critical.popleft()
                ok = False
            self.critical.append((self._seq, ev))
        self._cond.set()
        return ok

    def __len__(self) -> int:
        return len(self.ticks) + len(self.critical)

    def _pop(self) -> MarketEvent | None:
        if self.critical and (not self.ticks or self.critical[0][0] < self.ticks[0][0]):
            return self.critical.popleft()[1]
        if self.ticks:
            return self.ticks.popleft()[1]
        return None

    async def get(self) -> MarketEvent:
        while True:
            ev = self._pop()
            if ev is not None:
                return ev
            self._cond.clear()
            await self._cond.wait()


@dataclass
class Subscriptions:
    trades: set[str] = field(default_factory=set)
    quotes: set[str] = field(default_factory=set)
    bars: set[str] = field(default_factory=set)


ParseFn = Callable[[Any], list[MarketEvent | Exception]]


class ResilientStream:
    """Supervises one streaming connection and exposes a normalised event iterator."""

    def __init__(
        self,
        name: str,
        connect: Callable[[], Awaitable[Connection]],
        authenticate: Callable[[Connection], Awaitable[None]],
        build_subscribe: Callable[[Subscriptions], list[Any]],
        parse: ParseFn,
        feed: str = "",
        backoff: Backoff | None = None,
        stale_after: float = 30.0,
        tick_capacity: int = 10_000,
        dedupe_size: int = 100_000,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_auth_failures: int = 3,
    ):
        self.name, self.feed = name, feed
        self._connect, self._authenticate = connect, authenticate
        self._build_subscribe, self._parse = build_subscribe, parse
        self.backoff = backoff or Backoff()
        self.stale_after, self._sleep = stale_after, sleep
        self.subs = Subscriptions()
        self.buffer = EventBuffer(tick_capacity)
        self.health = ProviderHealth(provider=name, connected=False, feed=feed)
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._dedupe_size = dedupe_size
        self._last_bar: dict[str, Bar] = {}
        self._watermark: dict[str, Any] = {}
        self._conn: Connection | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._max_auth_failures = max_auth_failures
        self.fatal: str | None = None

    # -------------------------------------------------------------- subscriptions
    async def subscribe(
        self, trades: list[str] | None = None, quotes: list[str] | None = None, bars: list[str] | None = None
    ) -> None:
        self.subs.trades |= set(trades or [])
        self.subs.quotes |= set(quotes or [])
        self.subs.bars |= set(bars or [])
        if self._conn is not None:  # live connection: push incremental subscription now
            delta = Subscriptions(set(trades or []), set(quotes or []), set(bars or []))
            for msg in self._build_subscribe(delta):
                await self._conn.send(msg)

    # -------------------------------------------------------------------- output
    def _emit(self, ev: MarketEvent) -> None:
        if not self.buffer.put(ev):
            self.health.dropped_events += 1
            n = self.health.dropped_events
            if n == 1 or n % 1000 == 0:  # rate-limited so a flood of drops cannot itself flood the pipeline
                self.buffer.put(
                    ProviderEvent("backpressure", detail=f"{n} tick event(s) dropped so far (oldest first)")
                )

    def _emit_provider(self, kind: str, detail: str = "") -> None:
        self._emit(ProviderEvent(kind, detail=detail))  # type: ignore[arg-type]

    async def events(self) -> AsyncIterator[MarketEvent]:
        while not (self._stop.is_set() and len(self.buffer) == 0):
            try:
                yield await asyncio.wait_for(self.buffer.get(), timeout=0.5)
            except TimeoutError:
                continue

    # --------------------------------------------------------------------- ingest
    def _ingest(self, ev: MarketEvent) -> None:
        if not isinstance(ev, ProviderEvent):
            key = ev.event_key
            if key in self._seen:
                self.health.duplicate_events += 1
                return
            self._seen[key] = None
            if len(self._seen) > self._dedupe_size:
                self._seen.popitem(last=False)
        now = utcnow()
        self.health.last_ingest_ts = now
        ts = getattr(ev, "ts", None)
        if (
            ts is not None
            and not isinstance(ev, ProviderEvent)
            and (self.health.last_event_ts is None or ts > self.health.last_event_ts)
        ):
            self.health.last_event_ts = ts
        self.health.stale = False
        if isinstance(ev, Bar):
            self._track_bar(ev)
        elif isinstance(ev, Trade | Quote):
            wm = self._watermark.get(ev.symbol)
            if wm is not None and ev.ts < wm - timedelta(seconds=5):
                self.health.late_events += 1
            elif wm is None or ev.ts > wm:
                self._watermark[ev.symbol] = ev.ts
        elif isinstance(ev, SessionEvent):
            pass
        self._emit(ev)

    def _track_bar(self, bar: Bar) -> None:
        prev = self._last_bar.get(bar.symbol)
        if bar.corrected or bar.revision > 0:
            self.health.corrections += 1
        elif prev is not None:
            if bar.ts < prev.ts:
                self.health.late_events += 1
            else:
                missing = int((bar.ts - prev.ts).total_seconds() // 60) - 1
                same_day = et_date(bar.ts) == et_date(
                    prev.ts
                )  # gaps are only meaningful within one ET session
                if missing > 0 and same_day and missing < 60:
                    self.health.gaps += missing
                    self._emit_provider(
                        "gap", f"{bar.symbol}: {missing} missing 1-min bar(s) before {bar.ts.isoformat()}"
                    )
        if prev is None or bar.ts >= prev.ts:
            self._last_bar[bar.symbol] = bar

    # ---------------------------------------------------------------- supervisor
    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self.run(), name=f"{self.name}-stream")

    async def stop(self) -> None:
        self._stop.set()
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self.health.connected = False

    async def run(self) -> None:
        auth_failures = 0
        while not self._stop.is_set():
            wait = 0.0
            try:
                conn = await self._connect()
                self._conn = conn
                await self._authenticate(conn)
                for msg in self._build_subscribe(self.subs):  # resubscribe after (re)connect
                    await conn.send(msg)
                self.health.connected = True
                self._emit_provider("connected", "subscriptions restored" if self.health.reconnects else "")
                got_data = False
                while not self._stop.is_set():
                    try:
                        raw = await asyncio.wait_for(conn.recv(), timeout=self.stale_after)
                    except TimeoutError as exc:
                        self.health.stale = True
                        self._emit_provider("stale", f"no data for {self.stale_after:.0f}s; reconnecting")
                        raise StreamStale() from exc
                    for item in self._parse(raw):
                        if isinstance(item, Exception):
                            raise item
                        self._ingest(item)
                        got_data = True
                    if got_data:
                        self.backoff.reset()
                        auth_failures = 0  # only real data proves the credentials work
            except asyncio.CancelledError:
                raise
            except AuthError as exc:
                auth_failures += 1
                self._emit_provider(
                    "error", f"authentication failed ({auth_failures}/{self._max_auth_failures})"
                )
                if auth_failures >= self._max_auth_failures:
                    self.fatal = "authentication failed repeatedly; check data credentials"
                    self.health.detail = self.fatal
                    log.error("%s: %s", self.name, self.fatal)
                    self._stop.set()
                    del exc
                    break
                wait = self.backoff.next()
            except RateLimited as exc:
                self.health.rate_limited += 1
                wait = max(self.backoff.next(), exc.retry_after)
                self._emit_provider("rate_limited", f"backing off {wait:.1f}s")
            except StreamStale:
                wait = self.backoff.next()
            except Exception as exc:  # noqa: BLE001 - connection errors of any transport
                self._emit_provider("disconnected", type(exc).__name__)
                wait = self.backoff.next()
            finally:
                self.health.connected = False
                if self._conn is not None:
                    with contextlib.suppress(Exception):
                        await self._conn.close()
                    self._conn = None
            if self._stop.is_set():
                break
            self.health.reconnects += 1
            self._emit_provider("reconnecting", f"in {wait:.1f}s")
            await self._sleep(wait)
