from __future__ import annotations

import math
import socket
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy.exc import IntegrityError

from scanalert.alerts import AlertEvent
from scanalert.db import Store, make_engine, migrate
from scanalert.models import Bar, iso
from scanalert.paper import (
    FillConfig,
    IntentRejected,
    MarketSnapshot,
    PaperIntentRequest,
    build_intent,
    resolve_stop_target,
    simulate_fill,
)

NOW = datetime.fromisoformat("2026-09-28T14:00:00+00:00")


def alert(**kw) -> AlertEvent:
    base = dict(
        event_id="evt_1",
        symbol="AAA",
        strategy_id="s",
        strategy_version=3,
        condition_id="c",
        direction="long",
        event_type="x",
        source_timestamp=iso(NOW) or "",
        detected_timestamp=iso(NOW) or "",
        session="regular",
        trigger_price=10.0,
        bid=9.995,
        ask=10.005,
        spread_bps=10.0,
        status="triggered",
        feature_snapshot={"atr": 0.2},
    )
    base.update(kw)
    return AlertEvent(**base)


# ------------------------------------------------------------------------------ intents
def test_risk_based_sizing_hand_calculation():
    req = PaperIntentRequest(
        event_id="evt_1",
        risk_dollars=100,
        entry_model="spread_plus_slippage",
        slippage_bps=2,
        stop={"type": "percent", "value": 1},
    )
    it = build_intent(req, alert(), NOW)
    half, slip = 10.0 * 10 / 20000, 10.0 * 2 / 10000
    entry = 10.0 + half + slip
    stop_dist = entry * 0.01
    per_share = stop_dist + 2 * slip + 2 * half
    assert it.est_entry_price == pytest.approx(entry, abs=1e-4)
    assert it.quantity == math.floor(100 / per_share)
    assert (
        it.max_modeled_loss == pytest.approx(it.quantity * per_share, abs=0.01) and it.max_modeled_loss <= 100
    )
    assert it.stop_price == pytest.approx(entry - stop_dist, abs=1e-4)
    assert it.target_price == pytest.approx(entry + 2 * stop_dist, abs=1e-3)  # 2R
    assert it.simulated is True and it.submitted_to_broker is False and "NOT SUBMITTED" in it.label
    assert it.strategy_version == 3 and it.est_spread_bps == 10.0 and it.est_slippage_bps == 2


def test_short_intent_mirrors_prices():
    it = build_intent(
        PaperIntentRequest(event_id="evt_1", direction="short", quantity=50, entry_model="bid_ask_cross"),
        alert(),
        NOW,
    )
    assert (
        it.est_entry_price < 10.0
        and it.stop_price > it.est_entry_price
        and it.target_price < it.est_entry_price
    )


@pytest.mark.parametrize(
    ("stop", "target", "exp_stop", "exp_target"),
    [
        ({"type": "dollars", "value": 0.25}, {"type": "dollars", "value": 0.5}, 9.75, 10.5),
        ({"type": "percent", "value": 2}, {"type": "percent", "value": 4}, 9.8, 10.4),
        ({"type": "atr", "value": 2}, {"type": "r_multiple", "value": 1}, 9.6, 10.4),
    ],
)
def test_stop_and_target_models(stop, target, exp_stop, exp_target):
    it = build_intent(
        PaperIntentRequest(event_id="evt_1", quantity=10, entry_model="next_trade", stop=stop, target=target),
        alert(),
        NOW,
    )
    assert it.stop_price == pytest.approx(exp_stop) and it.target_price == pytest.approx(exp_target)


def test_intent_expiration_and_time_exit():
    it = build_intent(
        PaperIntentRequest(event_id="evt_1", quantity=10, expires_minutes=5, time_exit_minutes=30),
        alert(),
        NOW,
    )
    assert it.expires_at == iso(NOW + timedelta(minutes=5)) and it.time_exit_minutes == 30


@pytest.mark.parametrize("status", ["expired", "invalidated", "suppressed", "working"])
def test_intent_rejected_for_non_triggered_alert(status):
    with pytest.raises(IntentRejected):
        build_intent(PaperIntentRequest(event_id="evt_1", quantity=10), alert(status=status), NOW)


def test_intent_rejection_cases():
    with pytest.raises(IntentRejected, match="ATR"):
        build_intent(
            PaperIntentRequest(event_id="evt_1", quantity=1, stop={"type": "atr", "value": 1}),
            alert(feature_snapshot={}),
            NOW,
        )
    with pytest.raises(IntentRejected, match="below 1 share"):
        build_intent(PaperIntentRequest(event_id="evt_1", risk_dollars=0.01), alert(), NOW)
    with pytest.raises(IntentRejected):
        build_intent(PaperIntentRequest(event_id="evt_1", quantity=1), alert(trigger_price=None), NOW)
    with pytest.raises(IntentRejected, match="requires a stop"):
        build_intent(
            PaperIntentRequest(
                event_id="evt_1", quantity=1, stop={"type": "none"}, target={"type": "r_multiple", "value": 2}
            ),
            alert(),
            NOW,
        )


def test_request_validation():
    with pytest.raises(ValueError):
        PaperIntentRequest(quantity=1)  # neither event nor symbol
    with pytest.raises(ValueError):
        PaperIntentRequest(event_id="e", quantity=1, risk_dollars=1)
    with pytest.raises(ValueError):
        PaperIntentRequest(event_id="e")
    PaperIntentRequest(symbol="AAA", direction="long", reference_price=10, quantity=5)


def test_assumed_spread_flagged_when_alert_has_no_quote():
    it = build_intent(PaperIntentRequest(event_id="evt_1", quantity=10), alert(spread_bps=None), NOW)
    assert it.detail["spread_assumed"] is True and it.est_spread_bps == 10.0


# --------------------------------------------------------------------------------- fills
def snap(**kw) -> MarketSnapshot:
    base = dict(
        as_of=NOW,
        bid=9.99,
        ask=10.01,
        bid_size=500,
        ask_size=500,
        last=10.0,
        next_trade_price=10.02,
        next_trade_size=200,
        next_bar_open=10.03,
        next_bar_volume=10_000,
    )
    base.update(kw)
    return MarketSnapshot(**base)


def test_bid_ask_crossing_buy_at_ask_sell_at_bid():
    b = simulate_fill("i", "AAA", "buy", 100, "bid_ask_cross", snap())
    s = simulate_fill("i", "AAA", "sell", 100, "bid_ask_cross", snap())
    assert (b.price, s.price) == (10.01, 9.99) and b.simulated and b.status == "filled"


def test_fixed_slippage_is_adverse_each_side():
    cfg = FillConfig(slippage_bps=10)
    assert simulate_fill("i", "A", "buy", 10, "fixed_slippage", snap(), cfg).price == pytest.approx(10.01)
    assert simulate_fill("i", "A", "sell", 10, "fixed_slippage", snap(), cfg).price == pytest.approx(9.99)
    cents = FillConfig(fixed_slippage_cents=3)
    assert simulate_fill("i", "A", "buy", 10, "fixed_slippage", snap(), cents).price == pytest.approx(10.03)


def test_spread_plus_slippage():
    cfg = FillConfig(slippage_bps=10)
    assert simulate_fill("i", "A", "buy", 10, "spread_plus_slippage", snap(), cfg).price == pytest.approx(
        10.01 * 1.001, abs=1e-4
    )
    assert simulate_fill("i", "A", "sell", 10, "spread_plus_slippage", snap(), cfg).price == pytest.approx(
        9.99 * 0.999, abs=1e-4
    )


def test_next_trade_and_next_bar_open():
    assert simulate_fill("i", "A", "buy", 100, "next_trade", snap()).price == 10.02
    f = simulate_fill("i", "A", "buy", 100, "next_bar_open", snap())
    assert f.price == 10.03 and f.status == "filled"


def test_partial_fill_and_min_fraction_rejection():
    f = simulate_fill("i", "A", "buy", 1000, "bid_ask_cross", snap(ask_size=400))
    assert f.status == "partial" and f.partial and f.quantity == 400 and f.detail["unfilled_cancelled"] == 600
    r = simulate_fill("i", "A", "buy", 1000, "bid_ask_cross", snap(ask_size=100))
    assert r.status == "rejected" and "liquidity" in r.reject_reason and r.price is None and r.quantity == 0
    nop = simulate_fill(
        "i", "A", "buy", 1000, "bid_ask_cross", snap(ask_size=400), FillConfig(allow_partial=False)
    )
    assert nop.status == "rejected"


def test_participation_limits_next_bar_fill():
    f = simulate_fill(
        "i",
        "A",
        "buy",
        5000,
        "next_bar_open",
        snap(next_bar_volume=20_000),
        FillConfig(participation_rate=0.1),
    )
    assert f.status == "partial" and f.quantity == 2000


def test_stale_data_rejection():
    r = simulate_fill(
        "i", "A", "buy", 10, "bid_ask_cross", snap(data_age_seconds=45), FillConfig(max_data_age_seconds=15)
    )
    assert r.status == "rejected" and "stale" in r.reject_reason


def test_missing_data_and_unknown_model_rejected():
    assert simulate_fill("i", "A", "buy", 10, "bid_ask_cross", snap(ask=None)).status == "rejected"
    assert simulate_fill("i", "A", "buy", 10, "teleport", snap()).status == "rejected"


def test_fill_rows_are_always_simulated():
    f = simulate_fill("i", "A", "buy", 10, "next_trade", snap())
    row = f.as_row()
    assert f.simulated is True and "simulated" not in row and "never sent to a broker" in f.detail["note"]


# ------------------------------------------------------------------ stop/target collision
def bar(o, h, low, c):
    return Bar("A", NOW, o, h, low, c, 100)


@pytest.mark.parametrize(
    ("policy", "expected"),
    [("stop_first", ("stop", 9.9)), ("target_first", ("target", 10.2)), ("reject_ambiguous", "ambiguous")],
)
def test_ambiguous_bar_policies(policy, expected):
    assert resolve_stop_target("long", 9.9, 10.2, bar(10, 10.3, 9.8, 10), policy) == expected


def test_only_one_level_hit_ignores_policy():
    assert resolve_stop_target("long", 9.9, 10.2, bar(10, 10.1, 9.8, 10), "target_first") == ("stop", 9.9)
    assert resolve_stop_target("long", 9.9, 10.2, bar(10, 10.3, 9.95, 10), "stop_first") == ("target", 10.2)
    assert resolve_stop_target("long", 9.9, 10.2, bar(10, 10.1, 9.95, 10), "stop_first") is None


def test_gap_through_fills_at_open():
    assert resolve_stop_target("long", 9.9, 10.2, bar(9.7, 9.8, 9.6, 9.7), "target_first") == ("stop", 9.7)
    assert resolve_stop_target("long", 9.9, 10.2, bar(10.5, 10.6, 10.4, 10.5), "stop_first") == (
        "target",
        10.5,
    )


def test_short_side_collision():
    assert resolve_stop_target("short", 10.1, 9.8, bar(10, 10.2, 9.7, 10), "stop_first") == ("stop", 10.1)
    assert resolve_stop_target("short", 10.1, 9.8, bar(10, 10.05, 9.7, 10), "stop_first") == ("target", 9.8)


# --------------------------------------------------------------- no broker / DB guarantees
def test_creating_intent_never_touches_network(monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(a)
        raise AssertionError("network access attempted during paper intent creation")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(httpx.Client, "send", boom)
    monkeypatch.setattr(httpx.AsyncClient, "send", boom)
    it = build_intent(PaperIntentRequest(event_id="evt_1", risk_dollars=100), alert(), NOW)
    f = simulate_fill(it.intent_id, it.symbol, "buy", it.quantity, "bid_ask_cross", snap())
    assert not calls and f.simulated


def test_database_refuses_non_simulated_or_submitted_rows():
    eng = make_engine("sqlite://")
    migrate(eng)
    st = Store(eng, "fixture", "synthetic")
    it = build_intent(PaperIntentRequest(event_id="evt_1", quantity=5), alert(), NOW)
    st.save_paper_intent(it.as_row())
    got = st.get_paper_intent(it.intent_id)
    assert got["simulated"] is True and got["submitted_to_broker"] is False
    from sqlalchemy import text

    with pytest.raises(IntegrityError), eng.begin() as cx:
        cx.execute(
            text("UPDATE paper_intents SET submitted_to_broker=1 WHERE intent_id=:i"), {"i": it.intent_id}
        )
    with pytest.raises(IntegrityError), eng.begin() as cx:
        cx.execute(text("UPDATE paper_intents SET simulated=0 WHERE intent_id=:i"), {"i": it.intent_id})
    with pytest.raises(IntegrityError), eng.begin() as cx:
        cx.execute(
            text(
                "INSERT INTO alert_events (event_id,symbol,strategy_id,strategy_version,condition_id,direction,event_type,source_ts,detected_ts,session,status,priority,feature_snapshot,filter_snapshot,paper_only) VALUES ('x','A','s',1,'c','long','e','t','t','regular','triggered','normal','{}','{}',0)"
            )
        )
