"""Alert model, lifecycle state machine, deduplication, cooldowns and suppression audit."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from .calendar import et_date
from .models import iso, parse_ts, utcnow
from .scanner import Signal
from .strategy import PRIORITY_RANK

Status = Literal["working", "triggered", "invalidated", "expired", "acknowledged", "suppressed"]

TRANSITIONS: dict[str, set[str]] = {
    "working": {"triggered", "invalidated", "expired", "suppressed"},
    "triggered": {"acknowledged", "invalidated", "expired"},
    "acknowledged": set(),
    "invalidated": set(),
    "expired": set(),
    "suppressed": set(),
}
TERMINAL = {s for s, nxt in TRANSITIONS.items() if not nxt}


class InvalidTransition(ValueError):
    pass


def check_transition(current: str, target: str) -> None:
    if target not in TRANSITIONS.get(current, set()):
        raise InvalidTransition(f"illegal alert transition {current!r} -> {target!r}")


class AlertEvent(BaseModel):
    """Durable alert record. Field names follow the documented signal/event contract."""

    event_id: str
    symbol: str
    strategy_id: str
    strategy_version: int
    condition_id: str
    direction: Literal["long", "short"]
    event_type: str
    source_timestamp: str
    detected_timestamp: str
    session: str
    trigger_price: float | None
    bid: float | None = None
    ask: float | None = None
    spread_bps: float | None = None
    feature_snapshot: dict[str, Any] = Field(default_factory=dict)
    filter_snapshot: dict[str, Any] = Field(default_factory=dict)
    status: Status = "triggered"
    status_reason: str = ""
    priority: Literal["low", "normal", "high", "critical"] = "normal"
    dedupe_key: str = ""
    expires_at: str | None = None
    confirm_at: str | None = None
    acknowledged_at: str | None = None
    acknowledged_by: str | None = None
    delivered_timestamp: str | None = None
    delivery_delay_ms: int | None = None
    stale_data: bool = False
    delayed_delivery: bool = False
    data_provider: str = ""
    data_feed: str = ""
    strategy_config_hash: str = ""
    paper_only: bool = True
    hypothetical: bool = True
    label: str = "HYPOTHETICAL SIGNAL - PAPER ONLY - NOT AN ORDER"


def make_event_id(
    strategy_id: str, version: int, condition_id: str, symbol: str, source_ts: datetime, event_type: str
) -> str:
    raw = f"{strategy_id}|{version}|{condition_id}|{symbol}|{iso(source_ts)}|{event_type}"
    return "evt_" + hashlib.sha256(raw.encode()).hexdigest()[:20]


class AlertStore(Protocol):
    def save_alert(self, alert: AlertEvent) -> None: ...


class AlertManager:
    """Turns raw signals into persisted alerts, applying dedupe/cooldown/daily-cap suppression.

    Suppressed signals are *kept* (status ``suppressed`` with a reason) so skipped signals are auditable.
    """

    def __init__(
        self,
        store: AlertStore | None = None,
        clock: Callable[[], datetime] = utcnow,
        stale_seconds: float = 15.0,
        provider: str = "",
        feed: str = "",
        history_limit: int = 5000,
    ):
        self.store, self.clock = store, clock
        self.stale_seconds, self.provider, self.feed = stale_seconds, provider, feed
        self.alerts: dict[str, AlertEvent] = {}
        self._order: list[str] = []
        self._open: set[str] = set()  # non-terminal alerts (working/triggered)
        self._dedupe: set[str] = set()
        self._last_fire: dict[tuple[str, str, str], datetime] = {}
        self._daily: dict[tuple[str, str, str, Any], int] = defaultdict(int)
        self._history_limit = history_limit
        self._listeners: list[Callable[[AlertEvent], None]] = []

    def add_listener(self, fn: Callable[[AlertEvent], None]) -> None:
        self._listeners.append(fn)

    def _persist(self, a: AlertEvent) -> None:
        if self.store:
            self.store.save_alert(a)

    def _remember(self, a: AlertEvent) -> None:
        if a.event_id not in self.alerts:
            self._order.append(a.event_id)
        self.alerts[a.event_id] = a
        if a.status in TERMINAL:
            self._open.discard(a.event_id)
        else:
            self._open.add(a.event_id)
        if len(self._order) > self._history_limit:
            drop = self._order[: len(self._order) - self._history_limit]
            del self._order[: len(drop)]
            for d in drop:
                self.alerts.pop(d, None)
                self._open.discard(d)

    # ------------------------------------------------------------------------- creation
    def submit(self, sig: Signal) -> AlertEvent:
        cond, strat = sig.condition, sig.strategy
        detected = self.clock()  # wall clock live; simulated clock in replay/backtests
        bucket = int(sig.source_ts.timestamp() // cond.dedupe_bucket_seconds)
        dedupe_key = f"{sig.symbol}|{strat.id}|v{strat.version}|{cond.id}|{bucket}"
        event_id = make_event_id(strat.id, strat.version, cond.id, sig.symbol, sig.source_ts, cond.event_type)
        alert = AlertEvent(
            event_id=event_id,
            symbol=sig.symbol,
            strategy_id=strat.id,
            strategy_version=strat.version,
            condition_id=cond.id,
            direction=sig.direction,
            event_type=cond.event_type,
            source_timestamp=iso(sig.source_ts) or "",
            detected_timestamp=iso(detected) or "",
            session=sig.session,
            trigger_price=sig.trigger_price,
            bid=sig.bid,
            ask=sig.ask,
            spread_bps=sig.spread_bps,
            feature_snapshot=sig.feature_snapshot,
            filter_snapshot=sig.filter_snapshot,
            status="triggered",
            priority=cond.priority,
            dedupe_key=dedupe_key,
            expires_at=iso(sig.source_ts + timedelta(seconds=cond.expires_after_seconds)),
            stale_data=bool(sig.data_age_seconds is not None and sig.data_age_seconds > self.stale_seconds),
            data_provider=self.provider,
            data_feed=self.feed,
            strategy_config_hash=str(sig.filter_snapshot.get("config", {}).get("config_hash", "")),
        )
        if event_id in self.alerts:  # exact replay of the same source event
            return self.alerts[event_id]
        reason = self._suppression_reason(sig, dedupe_key, detected)
        if reason:
            alert.status, alert.status_reason = "suppressed", reason
            self._finish(alert)
            return alert
        self._dedupe.add(dedupe_key)
        self._last_fire[(sig.symbol, strat.id, cond.id)] = sig.source_ts
        self._daily[(sig.symbol, strat.id, cond.id, et_date(sig.source_ts))] += 1
        if cond.confirm_bars > 0:
            alert.status = "working"
            alert.confirm_at = iso(sig.source_ts + timedelta(minutes=cond.confirm_bars))
            alert.status_reason = f"awaiting confirmation for {cond.confirm_bars} bar(s)"
        self._finish(alert)
        return alert

    def _suppression_reason(self, sig: Signal, dedupe_key: str, now: datetime) -> str | None:
        cond, strat = sig.condition, sig.strategy
        if dedupe_key in self._dedupe:
            return f"duplicate: dedupe key {dedupe_key}"
        last = self._last_fire.get((sig.symbol, strat.id, cond.id))
        if (
            last is not None
            and cond.cooldown_seconds
            and (sig.source_ts - last).total_seconds() < cond.cooldown_seconds
        ):
            return f"cooldown: {cond.cooldown_seconds}s not elapsed since {iso(last)}"
        n = self._daily[(sig.symbol, strat.id, cond.id, et_date(sig.source_ts))]
        if n >= cond.max_alerts_per_symbol_per_day:
            return f"daily cap: {cond.max_alerts_per_symbol_per_day} alerts per symbol per day reached"
        return None

    def _finish(self, alert: AlertEvent) -> None:
        self._remember(alert)
        self._persist(alert)
        for fn in list(self._listeners):
            fn(alert)

    # ------------------------------------------------------------------------ lifecycle
    def _transition(self, event_id: str, target: Status, reason: str = "") -> AlertEvent:
        a = self.alerts.get(event_id)
        if a is None:
            raise KeyError(event_id)
        check_transition(a.status, target)
        a.status = target
        if target in TERMINAL:
            self._open.discard(event_id)
        if reason:
            a.status_reason = reason
        self._persist(a)
        for fn in list(self._listeners):
            fn(a)
        return a

    def acknowledge(self, event_id: str, by: str = "user") -> AlertEvent:
        a = self._transition(event_id, "acknowledged", "acknowledged by user")
        a.acknowledged_at, a.acknowledged_by = iso(self.clock()), by
        self._persist(a)
        return a

    def invalidate(self, event_id: str, reason: str) -> AlertEvent:
        return self._transition(event_id, "invalidated", reason)

    def expire(self, event_id: str, reason: str = "expired") -> AlertEvent:
        return self._transition(event_id, "expired", reason)

    def advance(self, now: datetime, still_true: Callable[[AlertEvent], bool | None]) -> list[AlertEvent]:
        """Move ``working`` alerts to triggered/invalidated/expired and expire stale ``triggered`` ones."""
        changed: list[AlertEvent] = []
        for eid in sorted(self._open):
            a = self.alerts.get(eid)
            if a is None or a.status in TERMINAL:
                continue
            exp = parse_ts(a.expires_at) if a.expires_at else None
            if a.status == "working":
                confirm = parse_ts(a.confirm_at) if a.confirm_at else now
                if exp and now >= exp:
                    changed.append(self._transition(eid, "expired", "confirmation window expired"))
                elif now >= confirm:
                    ok = still_true(a)
                    if ok:
                        changed.append(self._transition(eid, "triggered", "confirmed"))
                    else:
                        changed.append(
                            self._transition(eid, "invalidated", "condition no longer true at confirmation")
                        )
                elif still_true(a) is False:
                    changed.append(
                        self._transition(eid, "invalidated", "condition failed before confirmation")
                    )
            elif a.status == "triggered" and exp and now >= exp:
                changed.append(self._transition(eid, "expired", "alert expired unacknowledged"))
        return changed

    # ---------------------------------------------------------------------------- queries
    def get(self, event_id: str) -> AlertEvent | None:
        return self.alerts.get(event_id)

    def recent(self, limit: int = 100, status: str | None = None) -> list[AlertEvent]:
        out = [self.alerts[i] for i in reversed(self._order) if i in self.alerts]
        if status:
            out = [a for a in out if a.status == status]
        return out[:limit]


def priority_at_least(p: str, minimum: str) -> bool:
    return PRIORITY_RANK[p] >= PRIORITY_RANK[minimum]
