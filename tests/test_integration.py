"""Provider fixture replay -> scanner -> alerts -> persistence, with fault injection."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest

from scanalert.alerts import AlertEvent
from scanalert.config import Settings
from scanalert.db import Store, make_engine, migrate
from scanalert.features import FeatureContext
from scanalert.notify import NotConfigured, NotificationRouter, QuietHours, UserPreferences
from scanalert.providers.fixtures import FaultPlan, FixtureProvider
from scanalert.service import Service

pytestmark = pytest.mark.asyncio


def make_service(fx, faults=None, settings=None) -> Service:
    s = settings or Settings(database_url="sqlite://", fixture_autostart=False)
    eng = make_engine("sqlite://")
    migrate(eng)
    store = Store(eng, "fixture", "synthetic")
    return Service(s, store, FixtureProvider(fx, faults=faults))


async def run(fx, faults=None, until_min=None):
    svc = make_service(fx, faults)
    await svc.start(autostart_replay=False)
    until = (
        svc.provider.calendar.open_dt(svc.provider.live_days[0]) + timedelta(minutes=until_min)
        if until_min
        else None
    )
    await svc.replay_all(until)
    return svc


async def test_fixture_replay_produces_persisted_deterministic_alerts(fx):
    a = await run(fx)
    b = await run(fx)
    ids_a = [x.event_id for x in reversed(a.manager.recent(5000))]
    assert ids_a and ids_a == [x.event_id for x in reversed(b.manager.recent(5000))]
    persisted = {x.event_id: x for x in a.store.list_alerts(limit=1000)}
    assert set(persisted) == set(ids_a)
    triggered = [x for x in persisted.values() if x.status != "suppressed"]
    assert triggered and all(
        x.paper_only and x.feature_snapshot and x.filter_snapshot["config"]["config_hash"]
        for x in persisted.values()
    )
    assert a.store.count("bars") > 0 and a.store.count("raw_market_events") > 0
    assert a.replay_complete and a.store.count("market_sessions") > 0
    await a.stop()
    await b.stop()


async def test_alert_uses_source_and_delivery_timestamps_separately(fx):
    svc = await run(fx)
    orb = next(
        x
        for x in svc.manager.recent(500)
        if x.strategy_id == "opening-range-breakout" and x.status != "suppressed"
    )
    assert orb.source_timestamp <= orb.detected_timestamp
    deliveries = svc.store.list_deliveries(orb.event_id)
    assert (
        deliveries
        and deliveries[0]["status"] == "sent"
        and deliveries[0]["source_ts"] == orb.source_timestamp
    )
    await svc.stop()


async def test_fixture_replay_alerts_are_not_falsely_flagged_as_delayed(fx):
    svc = await run(fx)
    delivered = [a for a in svc.manager.recent(500) if a.status != "suppressed"]
    assert delivered
    assert all(
        a.delayed_delivery is False and a.delivery_delay_ms is not None and a.delivery_delay_ms <= 1000
        for a in delivered
    )
    await svc.stop()


async def test_faults_duplicates_late_and_corrections_do_not_corrupt_state(fx):
    clean = await run(fx)
    plan = FaultPlan(duplicate_every=7, correct_bar_at=120, late_bar_at=200, disconnect_after=1500)
    faulty = await run(fx, plan)
    first = sorted(fx.bars)[0]
    for sym in fx.bars:
        end = clean.clock()
        c = FeatureContext(clean.states[sym], end)
        f = FeatureContext(faulty.states[sym], end)
        assert f.get("volume") == c.get("volume"), sym  # duplicates never double count
        assert f.get("day_high") == c.get("day_high") and f.get("day_low") == c.get("day_low"), sym
    fs = faulty.states[first]
    assert fs.corrections_applied >= 1 and fs.late_bars >= 1
    kinds = [e["kind"] for e in faulty.provider_events]
    assert "disconnected" in kinds and "reconnecting" in kinds and "connected" in kinds
    assert faulty.provider_health_sync()["reconnects"] == 1
    corrected = faulty.store.load_bars(first)
    assert any(r["corrected"] == 1 for r in corrected)
    await clean.stop()
    await faulty.stop()


async def test_halt_suppresses_symbol_until_resume(fx):
    plan = FaultPlan(halt_at=60, resume_at=100)
    svc = make_service(fx, plan)
    await svc.start(autostart_replay=False)
    first = sorted(fx.bars)[0]
    open_dt = svc.provider.calendar.open_dt(svc.provider.live_days[0])
    await svc.replay_all(open_dt + timedelta(minutes=70))
    assert svc.states[first].halted is True
    await svc.replay_all()
    assert svc.states[first].halted is False
    await svc.stop()


async def test_top_list_from_replayed_state_is_ranked_and_fresh(fx):
    svc = await run(fx, until_min=200)
    snap = svc.compute_top_list("opening-range-breakout")
    assert snap.evaluated == len(fx.bars) and snap.stale is False
    scores = [r.score for r in snap.rows if r.score is not None]
    assert scores == sorted(scores, reverse=True) or snap.display_sort
    await svc.stop()


async def test_inactive_symbol_generates_no_alerts(fx):
    svc = make_service(fx)
    await svc.start(autostart_replay=False)
    for st in svc.states.values():
        st.active = False
    await svc.replay_all()
    assert svc.manager.recent(10) == []
    await svc.stop()


# ------------------------------------------------------------------------ notification rules
def alert(priority="normal", symbol="AAA", status="triggered", **kw) -> AlertEvent:
    return AlertEvent(
        event_id="evt_n",
        symbol=symbol,
        strategy_id="s",
        strategy_version=1,
        condition_id="c",
        direction="long",
        event_type="x",
        source_timestamp="2026-09-28T14:00:00.000Z",
        detected_timestamp="2026-09-28T14:00:00.050Z",
        session="regular",
        trigger_price=10.0,
        status=status,
        priority=priority,
        **kw,
    )  # type: ignore[arg-type]


class Rec:
    name = "browser"

    def __init__(self, fail_times=0, exc=RuntimeError("down")):
        self.calls, self.fail, self.exc = [], fail_times, exc

    async def send(self, payload):
        self.calls.append(payload)
        if len(self.calls) <= self.fail:
            raise self.exc


def router(ch, clock=None, **kw):
    from datetime import datetime

    sleeps = []

    async def sl(d):
        sleeps.append(d)

    r = NotificationRouter(
        {"browser": ch},
        clock=clock or (lambda: datetime.fromisoformat("2026-09-28T14:00:01+00:00")),
        sleep=sl,
        **kw,
    )
    r.sleeps = sleeps
    return r


async def test_retry_then_success_is_audited():
    ch = Rec(fail_times=2)
    r = router(ch)
    (d,) = await r.dispatch(alert(), UserPreferences(max_retries=3))
    assert d.status == "sent" and d.attempts == 3 and len(ch.calls) == 3 and r.sleeps == [0.5, 1.0]


async def test_retries_exhausted_recorded_as_failed():
    ch = Rec(fail_times=99)
    (d,) = await router(ch).dispatch(alert(), UserPreferences(max_retries=2))
    assert d.status == "failed" and d.attempts == 3 and "RuntimeError" in d.error and d.delivered_ts is None


async def test_not_configured_channel_fails_fast_without_retry():
    ch = Rec(fail_times=99, exc=NotConfigured("no smtp"))
    (d,) = await router(ch).dispatch(alert(), UserPreferences(max_retries=5))
    assert d.status == "failed" and d.attempts == 1


@pytest.mark.parametrize(
    ("prefs", "reason"),
    [
        (UserPreferences(browser_enabled=False), "channel disabled"),
        (UserPreferences(min_priority="high"), "below minimum priority"),
        (UserPreferences(muted_symbols=["AAA"]), "symbol muted"),
        (
            UserPreferences(quiet_hours=QuietHours(enabled=True, start="09:00", end="11:00")),
            "quiet hours",
        ),  # 14:00Z = 10:00 ET
    ],
)
async def test_notifications_skipped_with_reason(prefs, reason):
    ch = Rec()
    (d,) = await router(ch).dispatch(alert(), prefs)
    assert d.status == "skipped" and reason in d.reason and not ch.calls


async def test_quiet_hours_allow_critical_and_wrap_midnight():
    ch = Rec()
    q = UserPreferences(
        quiet_hours=QuietHours(enabled=True, start="09:00", end="11:00", allow_priority="critical")
    )
    (d,) = await router(ch).dispatch(alert(priority="critical"), q)
    assert d.status == "sent"
    from datetime import datetime

    wrap = QuietHours(enabled=True, start="22:00", end="07:00")
    assert (
        wrap.active(datetime.fromisoformat("2026-09-29T03:00:00+00:00")) is True
    )  # 03:00Z = 23:00 EDT: inside 22:00-07:00 (wraps midnight)
    assert (
        QuietHours(enabled=True, start="22:00", end="07:00").active(
            datetime.fromisoformat("2026-09-29T14:00:00+00:00")
        )
        is False
    )  # 10:00 ET


async def test_test_notifications_use_their_own_message_type():
    ch = Rec()
    await router(ch).dispatch(alert(), UserPreferences(), kind="test")
    await router(ch).dispatch(alert(), UserPreferences())
    assert [c["type"] for c in ch.calls] == ["test_notification", "alert"]


async def test_only_triggered_alerts_are_delivered():
    ch = Rec()
    for st in ("suppressed", "working", "expired", "acknowledged"):
        assert await router(ch).dispatch(alert(status=st), UserPreferences()) == []
    assert not ch.calls


async def test_delayed_delivery_flag_and_payload_warnings():
    from datetime import datetime

    ch = Rec()
    a = alert()
    r = router(ch, clock=lambda: datetime.fromisoformat("2026-09-28T14:00:05+00:00"), delayed_ms=2000)
    (d,) = await r.dispatch(a, UserPreferences())
    assert (
        d.latency_ms == 5000 and a.delayed_delivery and a.delivery_delay_ms == 5000 and a.delivered_timestamp
    )
    assert "DATA DELAY" in ch.calls[0]["warnings"] and ch.calls[0]["paper_only"] is True
    stale = alert(stale_data=True)
    await router(Rec()).dispatch(stale, UserPreferences())
    ch2 = Rec()
    await router(ch2).dispatch(stale, UserPreferences())
    assert "STALE FEED" in ch2.calls[0]["warnings"]


async def test_webhook_channel_refuses_broker_hosts():
    from scanalert.notify import WebhookChannel

    with pytest.raises(ValueError, match="live broker"):
        WebhookChannel("https://api.alpaca.markets/v2/orders")
    WebhookChannel("https://hooks.example.com/x")


async def test_hub_drops_oldest_for_slow_clients():
    from scanalert.notify import Hub

    h = Hub(queue_size=3)
    q = h.subscribe("alerts")
    for i in range(6):
        h.publish("alerts", {"i": i})
    assert h.dropped == 3 and [json.loads(q.get_nowait())["i"] for _ in range(3)] == [3, 4, 5]


async def test_email_and_webhook_disabled_by_default(fx):
    svc = make_service(fx)
    assert list(svc.router.channels) == ["browser"]
    on = Settings(
        database_url="sqlite://",
        email_enabled=True,
        webhook_enabled=True,
        webhook_url="https://hooks.example.com/x",
    )
    svc2 = make_service(fx, settings=on)
    assert set(svc2.router.channels) == {"browser", "email", "webhook"}
    await asyncio.sleep(0)
