"""Notification routing: WebSocket hub, user preferences, quiet hours, retries, delivery audit.

Notifications are non-transactional. No channel can place, route or modify an order; the webhook channel
refuses broker hosts via the same guard used at startup.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, Field

from .alerts import AlertEvent, priority_at_least
from .calendar import ET
from .config import inspect_url
from .models import iso, parse_ts, utcnow


class QuietHours(BaseModel):
    enabled: bool = False
    start: str = "22:00"  # ET, HH:MM
    end: str = "07:00"
    allow_priority: str = "critical"  # priorities at/above this still notify during quiet hours

    def active(self, now: datetime) -> bool:
        if not self.enabled:
            return False
        s = time.fromisoformat(self.start)
        e = time.fromisoformat(self.end)
        t = now.astimezone(ET).time()
        return (s <= t < e) if s <= e else (t >= s or t < e)


class UserPreferences(BaseModel):
    user_id: str = "default"
    browser_enabled: bool = True
    sound_enabled: bool = True
    email_enabled: bool = False
    webhook_enabled: bool = False
    min_priority: str = "low"
    quiet_hours: QuietHours = Field(default_factory=QuietHours)
    max_retries: int = Field(3, ge=0, le=10)
    muted_symbols: list[str] = Field(default_factory=list)


@dataclass
class Delivery:
    delivery_id: str
    event_id: str
    channel: str
    status: str  # sent | failed | skipped
    attempts: int
    reason: str
    error: str
    source_ts: str
    detected_ts: str
    delivered_ts: str | None
    latency_ms: int | None
    kind: str = "alert"  # alert | test

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class Channel(Protocol):
    name: str

    async def send(self, payload: dict[str, Any]) -> None: ...


class NotConfigured(RuntimeError):
    pass


class Hub:
    """Fan-out to WebSocket subscribers with bounded per-client queues (slow-client protection)."""

    def __init__(self, queue_size: int = 200):
        self.queue_size = queue_size
        self._subs: dict[str, set[asyncio.Queue[str]]] = {}
        self.dropped = 0

    def subscribe(self, topic: str) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=self.queue_size)
        self._subs.setdefault(topic, set()).add(q)
        return q

    def unsubscribe(self, topic: str, q: asyncio.Queue[str]) -> None:
        self._subs.get(topic, set()).discard(q)

    def subscriber_count(self, topic: str) -> int:
        return len(self._subs.get(topic, ()))

    def publish(self, topic: str, message: dict[str, Any]) -> int:
        data = json.dumps(message, default=str)
        n = 0
        for q in list(self._subs.get(topic, ())):
            if q.full():  # drop oldest for a slow client rather than blocking the pipeline
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
                self.dropped += 1
            q.put_nowait(data)
            n += 1
        return n


class WebSocketChannel:
    name = "browser"

    def __init__(self, hub: Hub):
        self.hub = hub

    async def send(self, payload: dict[str, Any]) -> None:
        self.hub.publish("alerts", payload)


class WebhookChannel:
    """Optional generic webhook (disabled by default). Never accepts broker hosts."""

    name = "webhook"

    def __init__(self, url: str, client: httpx.AsyncClient | None = None):
        problems = inspect_url(url) if url else []
        if problems:
            raise ValueError("; ".join(problems))
        self.url, self._client = url, client

    async def send(self, payload: dict[str, Any]) -> None:
        if not self.url:
            raise NotConfigured("WEBHOOK_URL is empty")
        client = self._client or httpx.AsyncClient(timeout=5.0)
        try:
            r = await client.post(self.url, json=payload)
            r.raise_for_status()
        finally:
            if self._client is None:
                await client.aclose()


class EmailChannel:
    """Interface placeholder: SMTP delivery is intentionally not implemented (disabled by default)."""

    name = "email"

    async def send(self, payload: dict[str, Any]) -> None:
        raise NotConfigured("email delivery is not configured")


DeliverySink = Callable[[Delivery], None]
Sleep = Callable[[float], Awaitable[None]]


class NotificationRouter:
    def __init__(
        self,
        channels: dict[str, Channel],
        sink: DeliverySink | None = None,
        clock: Callable[[], datetime] = utcnow,
        delayed_ms: int = 2000,
        sleep: Sleep = asyncio.sleep,
    ):
        self.channels, self.sink, self.clock = channels, sink, clock
        self.delayed_ms, self.sleep = delayed_ms, sleep
        self.audit: list[Delivery] = []

    def _record(self, d: Delivery) -> Delivery:
        self.audit.append(d)
        del self.audit[:-2000]
        if self.sink:
            self.sink(d)
        return d

    def _enabled(self, name: str, prefs: UserPreferences) -> bool:
        return {
            "browser": prefs.browser_enabled,
            "email": prefs.email_enabled,
            "webhook": prefs.webhook_enabled,
        }.get(name, False)

    async def dispatch(
        self, alert: AlertEvent, prefs: UserPreferences, kind: str = "alert", at: datetime | None = None
    ) -> list[Delivery]:
        """Deliver ``alert``. ``at`` pins the delivery time (fixture replay runs on a simulated clock)."""
        out: list[Delivery] = []
        if alert.status not in ("triggered",):
            return out
        now = at or self.clock()
        for name, channel in self.channels.items():
            base = dict(
                delivery_id="dlv_" + uuid.uuid4().hex[:16],
                event_id=alert.event_id,
                channel=name,
                source_ts=alert.source_timestamp,
                detected_ts=alert.detected_timestamp,
                kind=kind,
            )
            skip = None
            if not self._enabled(name, prefs):
                skip = "channel disabled"
            elif alert.symbol in prefs.muted_symbols:
                skip = "symbol muted"
            elif not priority_at_least(alert.priority, prefs.min_priority):
                skip = f"below minimum priority {prefs.min_priority}"
            elif prefs.quiet_hours.active(now) and not priority_at_least(
                alert.priority, prefs.quiet_hours.allow_priority
            ):
                skip = "quiet hours"
            if skip:
                out.append(
                    self._record(
                        Delivery(
                            status="skipped",
                            attempts=0,
                            reason=skip,
                            error="",
                            delivered_ts=None,
                            latency_ms=None,
                            **base,
                        )
                    )
                )
                continue
            attempts, err = 0, ""
            status = "failed"
            for attempt in range(prefs.max_retries + 1):
                attempts = attempt + 1
                try:
                    send_at = at or self.clock()
                    if name == "browser":  # lateness is known before sending so the payload can warn the user
                        lat = int((send_at - parse_ts(alert.source_timestamp)).total_seconds() * 1000)
                        alert.delivery_delay_ms = lat
                        alert.delayed_delivery = lat > self.delayed_ms
                    await channel.send(self._payload(alert, send_at, kind))
                    status, err = "sent", ""
                    break
                except Exception as exc:  # noqa: BLE001 - any channel failure is retried then audited
                    err = f"{type(exc).__name__}: {str(exc)[:200]}"
                    if isinstance(exc, NotConfigured):
                        break
                    if attempt < prefs.max_retries:
                        await self.sleep(min(2**attempt * 0.5, 8.0))
            delivered = (at or self.clock()) if status == "sent" else None
            latency = (
                int((delivered - parse_ts(alert.source_timestamp)).total_seconds() * 1000)
                if delivered
                else None
            )
            if delivered and name == "browser":
                alert.delivered_timestamp = iso(delivered)
                alert.delivery_delay_ms = latency
                alert.delayed_delivery = latency is not None and latency > self.delayed_ms
            out.append(
                self._record(
                    Delivery(
                        status=status,
                        attempts=attempts,
                        reason="",
                        error=err,
                        delivered_ts=iso(delivered),
                        latency_ms=latency,
                        **base,
                    )
                )
            )
        return out

    def _payload(self, alert: AlertEvent, now: datetime, kind: str = "alert") -> dict[str, Any]:
        return {
            # test notifications use their own type so clients never list them as (un-acknowledgeable) alerts
            "type": "test_notification" if kind == "test" else "alert",
            "alert": alert.model_dump(),
            "delivered_at": iso(now),
            "warnings": [
                w
                for w, on in (("STALE FEED", alert.stale_data), ("DATA DELAY", alert.delayed_delivery))
                if on
            ],
            "paper_only": True,
        }


@dataclass
class PreferenceStore:
    """In-memory preferences with optional persistence callback."""

    prefs: UserPreferences = field(default_factory=UserPreferences)
    persist: Callable[[UserPreferences], None] | None = None

    def update(self, prefs: UserPreferences) -> UserPreferences:
        self.prefs = prefs
        if self.persist:
            self.persist(prefs)
        return prefs
