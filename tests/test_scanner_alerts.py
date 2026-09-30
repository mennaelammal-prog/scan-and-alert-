from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from conftest import flat_day, mkbar
from scanalert.alerts import AlertManager, InvalidTransition, check_transition, make_event_id
from scanalert.calendar import NyseCalendar
from scanalert.features import SymbolState
from scanalert.filters import FilterSpec
from scanalert.models import Quote, parse_ts
from scanalert.scanner import EventScanner, SnapshotScanner
from scanalert.strategy import (
    AlertCondition,
    DisplaySort,
    RankingSpec,
    StrategySpec,
    TopListSpec,
    validate_strategy,
)

CAL = NyseCalendar()
HIST = [date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24), date(2026, 9, 25)]
TODAY = date(2026, 9, 28)


def flt(id_, field, op, value, **kw):
    return FilterSpec(id=id_, name=id_, field=field, operator=op, value=value, **kw)


def mkstate(sym="AAA", price=10.0, vol=1000.0) -> SymbolState:
    st = SymbolState(sym, CAL)
    for d in HIST:
        st.seed_history(flat_day(sym, d, price, vol, CAL))
    return st


def strat(**kw) -> StrategySpec:
    cond = AlertCondition(
        id="c1",
        name="c1",
        event_type="breakout",
        filters=[flt("px", "last", "gt", 10.4)],
        cooldown_seconds=kw.pop("cooldown", 600),
        dedupe_bucket_seconds=kw.pop("bucket", 300),
        max_alerts_per_symbol_per_day=kw.pop("cap", 3),
        confirm_bars=kw.pop("confirm", 0),
    )
    return StrategySpec(
        id="s1", name="s1", filters=[flt("g", "last", "gte", 5)], alert_conditions=[cond], **kw
    )


def step(st, i, close):
    m = CAL.regular_minutes(TODAY)[i]
    st.on_bar(mkbar(st.symbol, m, close, close, close, close, 5000))
    return m + timedelta(minutes=1)


def test_event_scanner_emits_on_transition_only():
    st, sc, s = mkstate(), EventScanner(CAL), strat()
    out = []
    for i, c in enumerate([10.0, 10.1, 10.5, 10.6, 10.7, 10.2, 10.6]):
        out.append(len(sc.evaluate(s, st, step(st, i, c))))
    assert out == [0, 0, 1, 0, 0, 0, 1]  # fires at 10.5, re-arms after dropping, fires again at 10.6


def test_disabled_strategy_and_inactive_or_halted_symbol_silent():
    st, sc = mkstate(), EventScanner(CAL)
    s = strat()
    assert sc.evaluate(s.model_copy(update={"enabled": False}), st, step(st, 0, 11)) == []
    st.active = False
    assert sc.evaluate(s, st, step(st, 1, 11)) == []
    st.active, st.halted = True, True
    assert sc.evaluate(s, st, step(st, 2, 11)) == []


def test_extended_hours_ignored_by_default():
    st, sc = mkstate(), EventScanner(CAL)
    pre = CAL.open_dt(TODAY) - timedelta(minutes=30)
    st.on_bar(mkbar("AAA", pre, 11, 11, 11, 11, 100))
    assert sc.evaluate(strat(), st, pre + timedelta(minutes=1)) == []


def test_signal_contains_full_contract_and_config_snapshot():
    st, sc, s = mkstate(), EventScanner(CAL), strat()
    as_of = step(st, 0, 10.5)
    st.on_quote(Quote("AAA", as_of, 10.49, 10.51, 100, 100))
    sig = sc.evaluate(s, st, as_of)[0]
    mgr = AlertManager(clock=lambda: as_of + timedelta(milliseconds=50))
    a = mgr.submit(sig)
    d = a.model_dump()
    for k in (
        "event_id",
        "symbol",
        "strategy_id",
        "strategy_version",
        "direction",
        "event_type",
        "source_timestamp",
        "detected_timestamp",
        "session",
        "trigger_price",
        "bid",
        "ask",
        "spread_bps",
        "feature_snapshot",
        "filter_snapshot",
        "status",
        "paper_only",
    ):
        assert k in d
    assert d["paper_only"] is True and d["status"] == "triggered" and d["session"] == "regular"
    assert d["spread_bps"] == pytest.approx(19.05, abs=0.1)
    assert d["filter_snapshot"]["config"]["config_hash"] == d["strategy_config_hash"]
    assert d["filter_snapshot"]["config"]["alert_conditions"][0]["filters"][0]["value"] == 10.4
    assert parse_ts(a.detected_timestamp) > parse_ts(a.source_timestamp)


def test_event_id_is_deterministic():
    t = datetime.fromisoformat("2026-09-28T14:00:00+00:00")
    assert make_event_id("s", 1, "c", "AAA", t, "e") == make_event_id("s", 1, "c", "AAA", t, "e")
    assert make_event_id("s", 2, "c", "AAA", t, "e") != make_event_id("s", 1, "c", "AAA", t, "e")


def run_signals(mgr, s, prices, sym="AAA"):
    st, sc = mkstate(sym), EventScanner(CAL)
    out = []
    for i, c in enumerate(prices):
        as_of = step(st, i, c)
        for sig in sc.evaluate(s, st, as_of):
            out.append(mgr.submit(sig))
    return out


def test_cooldown_suppresses_repeat_with_reason():
    mgr = AlertManager(clock=lambda: datetime.now().astimezone())
    out = run_signals(mgr, strat(cooldown=3600, bucket=1), [10.5, 10.0, 10.6, 10.0, 10.7])
    assert [a.status for a in out] == ["triggered", "suppressed", "suppressed"]
    assert "cooldown" in out[1].status_reason


def test_dedupe_key_suppresses_same_bucket():
    mgr = AlertManager(clock=lambda: datetime.now().astimezone())
    out = run_signals(mgr, strat(cooldown=0, bucket=3600), [10.5, 10.0, 10.6])
    assert [a.status for a in out] == ["triggered", "suppressed"] and "duplicate" in out[1].status_reason


def test_daily_cap():
    mgr = AlertManager(clock=lambda: datetime.now().astimezone())
    out = run_signals(mgr, strat(cooldown=0, bucket=1, cap=2), [10.5, 10, 10.5, 10, 10.5, 10, 10.5])
    assert [a.status for a in out] == ["triggered", "triggered", "suppressed", "suppressed"]
    assert "daily cap" in out[2].status_reason


def test_replaying_same_event_is_idempotent():
    mgr = AlertManager(clock=lambda: datetime.now().astimezone())
    s = strat()
    a1 = run_signals(mgr, s, [10.5])[0]
    a2 = run_signals(mgr, s, [10.5])[0]
    assert a1.event_id == a2.event_id and len(mgr.alerts) == 1


@pytest.mark.parametrize(
    ("cur", "tgt", "ok"),
    [
        ("working", "triggered", True),
        ("working", "invalidated", True),
        ("working", "expired", True),
        ("triggered", "acknowledged", True),
        ("triggered", "expired", True),
        ("triggered", "invalidated", True),
        ("acknowledged", "triggered", False),
        ("expired", "triggered", False),
        ("invalidated", "acknowledged", False),
        ("suppressed", "triggered", False),
        ("triggered", "working", False),
        ("working", "acknowledged", False),
    ],
)
def test_state_machine_transitions(cur, tgt, ok):
    if ok:
        check_transition(cur, tgt)
    else:
        with pytest.raises(InvalidTransition):
            check_transition(cur, tgt)


def test_acknowledge_and_terminal_states():
    mgr = AlertManager(clock=lambda: datetime.now().astimezone())
    a = run_signals(mgr, strat(), [10.5])[0]
    mgr.acknowledge(a.event_id)
    assert mgr.get(a.event_id).status == "acknowledged" and mgr.get(a.event_id).acknowledged_at
    with pytest.raises(InvalidTransition):
        mgr.acknowledge(a.event_id)
    with pytest.raises(InvalidTransition):
        mgr.invalidate(a.event_id, "x")


def test_working_alert_confirms_or_invalidates_and_expires():
    s = strat(confirm=2)
    t0 = CAL.open_dt(TODAY)
    now = [t0]
    mgr = AlertManager(clock=lambda: now[0])
    st, sc = mkstate(), EventScanner(CAL)
    as_of = step(st, 0, 10.5)
    a = mgr.submit(sc.evaluate(s, st, as_of)[0])
    assert a.status == "working" and a.confirm_at
    # still true at confirm time -> triggered
    as_of2 = step(st, 1, 10.6)
    now[0] = as_of + timedelta(minutes=2)
    changed = mgr.advance(now[0], lambda al: sc.still_true(s, al.condition_id, st, as_of2))
    assert [c.status for c in changed] == ["triggered"]
    # second alert becomes false before confirmation -> invalidated
    st2, sc2, mgr2 = mkstate("BBB"), EventScanner(CAL), AlertManager(clock=lambda: now[0])
    b = mgr2.submit(sc2.evaluate(s, st2, step(st2, 0, 10.5))[0])
    as_of3 = step(st2, 1, 10.0)
    ch = mgr2.advance(as_of3, lambda al: sc2.still_true(s, al.condition_id, st2, as_of3))
    assert mgr2.get(b.event_id).status == "invalidated" and ch
    # triggered alert expires after ttl
    mgr3 = AlertManager(clock=lambda: now[0])
    st3, sc3 = mkstate("CCC"), EventScanner(CAL)
    c3 = mgr3.submit(sc3.evaluate(strat(), st3, step(st3, 0, 10.5))[0])
    mgr3.advance(parse_ts(c3.expires_at) + timedelta(seconds=1), lambda a: True)
    assert mgr3.get(c3.event_id).status == "expired"


def test_stale_data_flag_set_from_feed_age():
    st, sc = mkstate(), EventScanner(CAL)
    step(st, 0, 10.5)
    late_as_of = CAL.regular_minutes(TODAY)[0] + timedelta(minutes=10)  # evaluated long after last event
    sig = sc.evaluate(strat(), st, late_as_of)[0]
    assert AlertManager(stale_seconds=15).submit(sig).stale_data is True


# ------------------------------------------------------------------------ snapshot scanner
def universe(prices_vols):
    return {s: mkstate(s, p, 1000) for s, (p, v) in prices_vols.items()}


def load(states, vols):
    m = CAL.regular_minutes(TODAY)[0]
    for s, st in states.items():
        st.on_bar(mkbar(s, m, 10, 10, 10, 10, vols[s]))
    return m + timedelta(minutes=1)


def top_spec(**kw):
    return StrategySpec(
        id="t",
        name="t",
        filters=[flt("g", "last", "gte", 5)],
        ranking=kw.pop("ranking", RankingSpec(field="rvol", order="desc")),
        **kw,
    )


def test_ranking_desc_with_deterministic_tie_break():
    vols = {"BBB": 3000.0, "AAA": 3000.0, "CCC": 5000.0, "DDD": 1000.0}
    st = {s: mkstate(s) for s in vols}
    as_of = load(st, vols)
    snap = SnapshotScanner(CAL).run(top_spec(), st, as_of)
    assert [r.symbol for r in snap.rows] == [
        "CCC",
        "AAA",
        "BBB",
        "DDD",
    ]  # ties (AAA,BBB) broken by symbol asc
    again = SnapshotScanner(CAL).run(top_spec(), dict(reversed(list(st.items()))), as_of)
    assert [r.symbol for r in again.rows] == [r.symbol for r in snap.rows]


def test_ranking_ascending_and_formula_and_max_rows():
    vols = {"AAA": 1000.0, "BBB": 2000.0, "CCC": 4000.0}
    st = {s: mkstate(s) for s in vols}
    as_of = load(st, vols)
    asc = SnapshotScanner(CAL).run(top_spec(ranking=RankingSpec(field="rvol", order="asc")), st, as_of)
    assert [r.symbol for r in asc.rows] == ["AAA", "BBB", "CCC"]
    fm = SnapshotScanner(CAL).run(top_spec(ranking=RankingSpec(formula="rvol * -1", order="desc")), st, as_of)
    assert [r.symbol for r in fm.rows] == ["AAA", "BBB", "CCC"]
    lim = SnapshotScanner(CAL).run(top_spec(top_list=TopListSpec(max_rows=2)), st, as_of)
    assert len(lim.rows) == 2 and lim.qualified == 3


def test_display_sort_is_secondary_and_applied_after_server_rank():
    vols = {"AAA": 1000.0, "BBB": 2000.0, "CCC": 4000.0}
    st = {s: mkstate(s) for s in vols}
    as_of = load(st, vols)
    snap = SnapshotScanner(CAL).run(
        top_spec(top_list=TopListSpec(max_rows=2), display_sort=DisplaySort(field="symbol", order="asc")),
        st,
        as_of,
    )
    assert [r.symbol for r in snap.rows] == ["BBB", "CCC"]  # top-2 by rvol (CCC,BBB) re-sorted by symbol
    assert {r.symbol: r.rank for r in snap.rows} == {"CCC": 1, "BBB": 2}  # server rank preserved


def test_filters_gate_the_top_list_and_null_scores_rank_last():
    st = {"AAA": mkstate("AAA"), "LOW": mkstate("LOW", price=2.0), "NEW": SymbolState("NEW", CAL)}
    m = CAL.regular_minutes(TODAY)[0]
    st["AAA"].on_bar(mkbar("AAA", m, 10, 10, 10, 10, 3000))
    st["LOW"].on_bar(mkbar("LOW", m, 2, 2, 2, 2, 9000))  # fails the price >= 5 gate
    st["NEW"].on_bar(mkbar("NEW", m, 10, 10, 10, 10, 100))  # no history -> rvol null
    snap = SnapshotScanner(CAL).run(top_spec(), st, m + timedelta(minutes=1))
    assert [r.symbol for r in snap.rows] == ["AAA", "NEW"] and snap.rows[-1].score is None


def test_snapshot_reports_freshness_and_labels():
    st = {"AAA": mkstate("AAA")}
    as_of = load(st, {"AAA": 3000.0}) + timedelta(seconds=120)
    d = SnapshotScanner(CAL, stale_seconds=15).run(top_spec(), st, as_of).as_dict()
    assert d["stale"] is True and d["paper_only"] is True and "NOT AN ORDER" in d["label"]


def test_strategy_validation_reports_clear_errors():
    bad = validate_strategy(
        {
            "id": "x",
            "name": "x",
            "filters": [{"id": "a", "name": "a", "field": "nope", "operator": "gt", "value": 1}],
        }
    )
    assert not bad["valid"] and "unknown field" in bad["errors"][0]["message"]
    empty = validate_strategy({"id": "x", "name": "x"})
    assert not empty["valid"]
    ok = validate_strategy(
        {
            "id": "x",
            "name": "x",
            "filters": [{"id": "a", "name": "a", "field": "last", "operator": "gt", "value": 1}],
        }
    )
    assert ok["valid"] and ok["warnings"]
    dup = validate_strategy(
        {
            "id": "x",
            "name": "x",
            "filters": [{"id": "a", "name": "a", "field": "last", "operator": "gt", "value": 1}] * 2,
        }
    )
    assert not dup["valid"]


def test_config_snapshot_changes_with_any_filter_edit():
    a, b = strat(), strat()
    assert a.config_snapshot()["config_hash"] == b.config_snapshot()["config_hash"]
    c = b.model_copy(deep=True)
    c.alert_conditions[0].filters[0].value = 10.5
    assert c.config_snapshot()["config_hash"] != a.config_snapshot()["config_hash"]
