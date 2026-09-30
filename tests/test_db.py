from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from scanalert.alerts import AlertEvent
from scanalert.db import Store, make_engine, migrate
from scanalert.models import Bar, iso, utcnow
from scanalert.notify import Delivery, UserPreferences
from scanalert.strategy import momentum_gap, opening_range_breakout

REQUIRED = {
    "symbols",
    "market_sessions",
    "raw_market_events",
    "quotes",
    "trades",
    "bars",
    "strategies",
    "strategy_versions",
    "filters",
    "formula_versions",
    "alert_events",
    "alert_deliveries",
    "backtest_runs",
    "backtest_trades",
    "paper_intents",
    "paper_fills",
    "audit_log",
}
T0 = datetime.fromisoformat("2026-09-28T14:00:00+00:00")


@pytest.fixture()
def store():
    eng = make_engine("sqlite://")
    migrate(eng)
    return Store(eng, "fixture", "synthetic")


def test_migrations_create_all_required_tables_and_indexes(store):
    assert store.table_names() >= REQUIRED
    idx = {
        i["name"]
        for t in ("alert_events", "bars", "raw_market_events", "paper_intents")
        for i in inspect(store.engine).get_indexes(t)
    }
    assert {
        "ix_alerts_source_ts",
        "ix_alerts_symbol",
        "ix_alerts_strategy",
        "ix_bars_ts",
        "ix_raw_events_symbol_ts",
    } <= idx


def test_migrations_idempotent_and_checksummed(store):
    assert migrate(store.engine) == []
    with store.engine.begin() as cx:
        cx.execute(text("UPDATE schema_migrations SET checksum='tampered'"))
    with pytest.raises(RuntimeError, match="modified after being applied"):
        migrate(store.engine)


def test_file_database_persists_across_engines(tmp_path):
    url = f"sqlite:///{tmp_path}/d.db"
    e1 = make_engine(url)
    migrate(e1)
    Store(e1).create_strategy(opening_range_breakout())
    e1.dispose()
    e2 = make_engine(url)
    assert migrate(e2) == [] and [s.id for s in Store(e2).list_strategies()] == ["opening-range-breakout"]


def test_strategy_versions_are_immutable_history(store):
    s1 = store.create_strategy(opening_range_breakout())
    assert s1.version == 1
    edited = s1.model_copy(deep=True)
    edited.alert_conditions[0].filters[1].value = 3.0
    s2 = store.update_strategy(s1.id, edited)
    assert s2.version == 2
    assert store.get_strategy(s1.id, 1).alert_conditions[0].filters[1].value == 1.5  # v1 untouched
    assert store.get_strategy(s1.id).alert_conditions[0].filters[1].value == 3.0
    vers = store.strategy_versions(s1.id)
    assert [v["version"] for v in vers] == [1, 2] and vers[0]["config_hash"] != vers[1]["config_hash"]
    with pytest.raises(ValueError, match="already exists"):
        store.create_strategy(s1)
    with pytest.raises(KeyError):
        store.update_strategy("nope", s1)


def test_filters_and_formulas_persisted_per_version(store):
    s = store.create_strategy(momentum_gap())
    rows = store._all(
        "SELECT scope, filter_id, field, operator, null_policy FROM filters WHERE strategy_id=:i AND strategy_version=1",
        {"i": s.id},
    )
    assert {r["filter_id"] for r in rows} >= {"min-price", "gap", "rvol", "hold-vwap", "new-high-30"}
    assert {r["scope"] for r in rows} == {"gate", "condition:new-high"}
    fv = store._all("SELECT formula_id, digest FROM formula_versions WHERE strategy_id=:i", {"i": s.id})
    assert {r["formula_id"] for r in fv} == {"gate:hold-vwap", "ranking"} and all(
        len(r["digest"]) == 16 for r in fv
    )


def mk_alert(**kw) -> AlertEvent:
    base = dict(
        event_id="evt_a",
        symbol="AAA",
        strategy_id="s",
        strategy_version=2,
        condition_id="c",
        direction="long",
        event_type="x",
        source_timestamp=iso(T0) or "",
        detected_timestamp=iso(T0 + timedelta(milliseconds=30)) or "",
        session="regular",
        trigger_price=10.0,
        bid=9.99,
        ask=10.01,
        spread_bps=20.0,
        feature_snapshot={"rvol": 2.5, "vwap": None},
        filter_snapshot={"config": {"config_hash": "abc"}},
        strategy_config_hash="abc",
        data_provider="fixture",
        data_feed="synthetic",
    )
    base.update(kw)
    return AlertEvent(**base)


def test_alert_round_trip_preserves_everything_and_updates_status(store):
    a = mk_alert()
    store.save_alert(a)
    got = store.get_alert("evt_a")
    assert got.model_dump() == a.model_dump()
    a.status, a.acknowledged_at, a.acknowledged_by = "acknowledged", iso(T0), "me"
    store.save_alert(a)
    assert store.get_alert("evt_a").status == "acknowledged" and store.count("alert_events") == 1
    store.save_alert(mk_alert(event_id="evt_b", symbol="BBB", status="suppressed", status_reason="cooldown"))
    assert [x.event_id for x in store.list_alerts(status="suppressed")] == ["evt_b"]
    assert [x.event_id for x in store.list_alerts(symbol="AAA")] == ["evt_a"]


def test_alert_status_check_constraint(store):
    store.save_alert(mk_alert())
    with pytest.raises(IntegrityError):
        store._exec("UPDATE alert_events SET status='bogus'")


def test_delivery_audit_round_trip(store):
    d = Delivery(
        "dlv_1",
        "evt_a",
        "browser",
        "sent",
        2,
        "",
        "",
        iso(T0) or "",
        iso(T0) or "",
        iso(T0 + timedelta(seconds=1)),
        1000,
    )
    store.save_delivery(d)
    store.save_delivery(d)  # idempotent
    rows = store.list_deliveries("evt_a")
    assert len(rows) == 1 and rows[0]["attempts"] == 2 and rows[0]["latency_ms"] == 1000


def test_bars_upsert_and_latest_revision_wins(store):
    b = Bar("AAA", T0, 10, 10.1, 9.9, 10.0, 100)
    store.save_bars([b, b])
    store.save_bars([Bar("AAA", T0, 10, 10.2, 9.9, 10.1, 150, revision=1, corrected=True)])
    assert store.count("bars") == 2
    latest = store.load_bars("AAA")
    assert len(latest) == 1 and latest[0]["close"] == 10.1 and latest[0]["corrected"] == 1


def test_raw_events_keep_both_timestamps_and_dedupe(store):
    from scanalert.models import Trade

    t = Trade("AAA", T0, 10.0, 100, "1", ingest_ts=T0 + timedelta(milliseconds=45))
    store.save_raw_events([t, t])
    rows = store._all("SELECT source_ts, ingest_ts, kind, provider, feed FROM raw_market_events")
    assert (
        len(rows) == 1 and rows[0]["source_ts"] != rows[0]["ingest_ts"] and rows[0]["provider"] == "fixture"
    )


def test_symbols_activation_and_sessions(store, cal):
    store.upsert_symbols(["AAA", "BBB"])
    store.upsert_symbols(["AAA"])
    store.set_symbol_active("BBB", False, "delisted")
    rows = {r["symbol"]: r for r in store.list_symbols()}
    assert rows["AAA"]["active"] == 1 and rows["BBB"]["active"] == 0 and rows["BBB"]["status"] == "delisted"
    from datetime import date

    store.record_sessions(cal, [date(2026, 9, 7), date(2026, 11, 27), date(2026, 9, 28)])
    ms = {r["session_date"]: r for r in store._all("SELECT * FROM market_sessions")}
    assert ms["2026-09-07"]["is_trading_day"] == 0 and ms["2026-09-07"]["holiday_name"] == "Labor Day"
    assert ms["2026-11-27"]["early_close"] == 1 and ms["2026-09-28"]["early_close"] == 0


def test_preferences_and_audit(store):
    p = UserPreferences(min_priority="high", muted_symbols=["ZZZ"])
    store.save_preferences(p)
    assert (
        store.load_preferences().min_priority == "high"
        and store.load_preferences("other").min_priority == "low"
    )
    store.audit("u", "act", "thing", "1", "detail")
    assert store.list_audit()[0]["action"] == "act"


def test_backtest_run_and_trades_round_trip(store):
    spec = store.create_strategy(opening_range_breakout())
    store.create_backtest_run(
        "bt_1", spec, {"start_date": "2026-09-01", "end_date": "2026-09-10"}, "fixture", "synthetic"
    )
    assert store.get_backtest_run("bt_1")["status"] == "running"
    trades = [
        {
            "symbol": "AAA",
            "direction": "long",
            "entry_ts": "a",
            "entry_price": 1,
            "exit_ts": "b",
            "exit_price": 2,
            "quantity": 5,
            "gross_pnl": 5,
            "costs": 0.5,
            "net_pnl": 4.5,
            "exit_reason": "profit_target",
            "event_id": "e",
        }
    ]
    store.save_backtest_trades("bt_1", trades)
    store.finish_backtest_run("bt_1", {"metrics": {"total_trades": 1}})
    run = store.get_backtest_run("bt_1")
    assert (
        run["status"] == "completed"
        and run["report"]["metrics"]["total_trades"] == 1
        and run["strategy_snapshot"]["config_hash"] == run["config_hash"]
    )
    assert (
        run["simulated"] == 1
        and "HISTORICAL SIMULATION" in run["label"]
        and run["data_provider"] == "fixture"
    )
    assert store.list_backtest_trades("bt_1")[0]["net_pnl"] == 4.5
    store.create_backtest_run("bt_2", spec, {}, "fixture", "synthetic")
    store.finish_backtest_run("bt_2", None, error="boom")
    assert store.get_backtest_run("bt_2")["status"] == "failed"


def test_foreign_keys_enforced(store):
    with pytest.raises(IntegrityError):
        store.save_backtest_trades(
            "missing-run",
            [
                {
                    "symbol": "A",
                    "direction": "long",
                    "entry_ts": "a",
                    "entry_price": 1,
                    "exit_ts": "b",
                    "exit_price": 1,
                    "quantity": 1,
                    "gross_pnl": 0,
                    "costs": 0,
                    "net_pnl": 0,
                    "exit_reason": "x",
                }
            ],
        )


def test_utcnow_is_aware():
    assert utcnow().tzinfo is not None
