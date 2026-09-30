"""End-to-end: create strategy -> validate -> replay fixture data -> live-style alert over WebSocket ->
acknowledge -> paper intent -> backtest -> report."""

from __future__ import annotations

import socket

import pytest
from fastapi.testclient import TestClient

from scanalert.api import create_app
from scanalert.config import Settings


@pytest.fixture(scope="module")
def client():
    app = create_app(Settings(database_url="sqlite://", fixture_autostart=False))
    with TestClient(app) as c:
        yield c


E2E = {
    "id": "e2e-orb",
    "name": "E2E ORB",
    "direction": "long",
    "universe": ["ORBX"],
    "filters": [
        {
            "id": "min-price",
            "name": "Price >= 5",
            "field": "last",
            "operator": "gte",
            "value": 5,
            "unit": "usd",
        }
    ],
    "alert_conditions": [
        {
            "id": "orb",
            "name": "ORB up",
            "event_type": "breakout",
            "priority": "high",
            "cooldown_seconds": 600,
            "filters": [
                {
                    "id": "orb-up",
                    "name": "orb",
                    "field": "orb_breakout_up",
                    "operator": "is_true",
                    "lookback": 15,
                },
                {"id": "rvol", "name": "rvol", "field": "rvol", "operator": "gte", "value": 1.5},
                {
                    "id": "f",
                    "name": "formula",
                    "field": "formula",
                    "operator": "is_true",
                    "formula": "vwap_dist_pct > 0.1 and ret(5) > 0",
                },
            ],
        }
    ],
    "ranking": {"field": "rvol", "order": "desc"},
}


def test_health_config_and_features(client):
    h = client.get("/health").json()
    assert (
        h["status"] == "ok"
        and h["paper_only"] is True
        and h["trading_mode"] == "paper"
        and "PAPER ONLY" in h["labels"]
    )
    assert h["data_provider"]["provider"] == "fixture" and "market_session" in h and "last_event_time" in h
    c = client.get("/api/config/public").json()
    assert c["live_trading_supported"] is False and c["order_endpoints"] == [] and c["paper_only"] is True
    assert "secret" not in str(c).lower().replace("alpaca_data_secret_key", "")
    feats = {f["name"]: f for f in client.get("/api/features").json()}
    assert {"rvol", "vwap_dist_pct", "orb_breakout_up", "spread_bps", "sma"} <= set(feats) and feats["sma"][
        "default_lookback"
    ] == 20
    assert client.get("/docs").status_code == 200 and "paths" in client.get("/openapi.json").json()


def test_strategy_crud_validate_versioning(client):
    bad = client.post(
        "/api/strategies/e2e-orb/validate",
        json={**E2E, "filters": [{"id": "x", "name": "x", "field": "nope", "operator": "gt", "value": 1}]},
    ).json()
    assert bad["valid"] is False and "unknown field" in bad["errors"][0]["message"]
    badf = {
        **E2E,
        "alert_conditions": [
            {
                **E2E["alert_conditions"][0],
                "filters": [
                    {
                        "id": "f",
                        "name": "f",
                        "field": "formula",
                        "operator": "is_true",
                        "formula": "last + pct_change > 1",
                    }
                ],
            }
        ],
    }
    assert "unit mismatch" in str(client.post("/api/strategies/e2e-orb/validate", json=badf).json()["errors"])
    ok = client.post("/api/strategies/e2e-orb/validate", json=E2E).json()
    assert ok["valid"] is True
    assert client.post("/api/strategies", json=badf).status_code == 422
    r = client.post("/api/strategies", json=E2E)
    assert r.status_code == 201 and r.json()["version"] == 1 and len(r.json()["config_hash"]) == 16
    assert client.post("/api/strategies", json=E2E).status_code == 409
    upd = {**E2E, "name": "E2E ORB renamed"}
    r2 = client.put("/api/strategies/e2e-orb", json=upd)
    assert r2.status_code == 200 and r2.json()["version"] == 2
    assert [v["version"] for v in client.get("/api/strategies/e2e-orb/versions").json()] == [1, 2]
    assert client.get("/api/strategies/e2e-orb?version=1").json()["name"] == "E2E ORB"
    assert client.put("/api/strategies/ghost", json=E2E).status_code == 404
    assert "e2e-orb" in [s["id"] for s in client.get("/api/strategies").json()]
    assert client.post("/api/strategies/e2e-orb/validate").json()["valid"] is True  # validates stored spec


def test_full_flow_alert_ws_ack_paper_backtest(client):
    with client.websocket_connect("/ws/alerts") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["paper_only"] is True
        r = client.post("/api/dev/replay/run", params={"until_minutes": 30})
        assert r.status_code == 200 and r.json()["replay_complete"] is False
        got = None
        for _ in range(50):
            msg = ws.receive_json()
            if msg["type"] == "alert" and msg["alert"]["strategy_id"] == "e2e-orb":
                got = msg
                break
        assert got and got["paper_only"] is True
        alert = got["alert"]
        assert alert["symbol"] == "ORBX" and alert["status"] == "triggered" and alert["strategy_version"] == 2
        assert (
            alert["feature_snapshot"]["rvol"] >= 1.5
            and alert["filter_snapshot"]["config"]["config_hash"] == alert["strategy_config_hash"]
        )
        eid = alert["event_id"]

        # list/detail/filter
        rows = client.get("/api/alerts", params={"strategy_id": "e2e-orb"}).json()
        assert [x["event_id"] for x in rows] == [eid]
        d = client.get(f"/api/alerts/{eid}").json()
        assert d["alert"]["event_id"] == eid and d["deliveries"][0]["channel"] == "browser"
        chart = client.get(f"/api/alerts/{eid}/chart").json()
        assert len(chart["bars"]) > 20 and chart["trigger_price"] == alert["trigger_price"]

        # top list (separate from event alerts)
        tl = client.get("/api/top-lists/e2e-orb").json()
        assert tl["paper_only"] is True and tl["refresh_seconds"] == 30 and "NOT AN ORDER" in tl["label"]

        # paper intent while alert is live: simulated only, no network
        orig = socket.socket.connect
        socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network used"))  # type: ignore[method-assign]
        try:
            pi = client.post(
                "/api/paper-intents",
                json={"event_id": eid, "risk_dollars": 100, "entry_model": "spread_plus_slippage"},
            )
        finally:
            socket.socket.connect = orig  # type: ignore[method-assign]
        assert pi.status_code == 201
        body = pi.json()
        assert (
            body["broker_submission"] is False
            and body["simulated"] is True
            and body["intent"]["submitted_to_broker"] is False
        )
        assert body["intent"]["strategy_version"] == 2 and body["fills"][0]["status"] in ("filled", "partial")
        iid = body["intent"]["intent_id"]
        assert client.get("/api/paper-fills", params={"intent_id": iid}).json()[0]["simulated"] is True
        assert client.get("/api/paper-positions").json()["positions"][0]["simulated"] is True

        # acknowledge via REST then via WS ack on a second call fails with 409
        a = client.post(f"/api/alerts/{eid}/acknowledge")
        assert a.status_code == 200 and a.json()["status"] == "acknowledged"
        assert client.post(f"/api/alerts/{eid}/acknowledge").status_code == 409
        upd = None
        for _ in range(20):
            m = ws.receive_json()
            if m["type"] == "alert_update" and m["alert"]["event_id"] == eid:
                upd = m
                break
        assert upd and upd["alert"]["status"] == "acknowledged"
        ws.send_json({"type": "ack", "event_id": eid})
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"

    # finish the day, then backtest and read the report
    assert client.post("/api/dev/replay/run").json()["replay_complete"] is True
    bt = client.post(
        "/api/backtests",
        json={
            "strategy_id": "e2e-orb",
            "wait": True,
            "config": {
                "start_date": "2026-09-08",
                "end_date": "2026-09-25",
                "assumed_spread_bps_by_symbol": {"ORBX": 6},
                "exits": {
                    "profit_target": {"type": "percent", "value": 1.5},
                    "stop_loss": {"type": "percent", "value": 0.75},
                    "time_after_entry_minutes": 90,
                },
                "costs": {"commission_per_share": 0.005, "spread_bps": 5, "slippage_bps": 2},
                "intrabar_policy": "stop_first",
            },
        },
    )
    assert bt.status_code == 202 and bt.json()["status"] == "completed"
    rid = bt.json()["run_id"]
    run = client.get(f"/api/backtests/{rid}").json()
    rep = run["report"]
    assert (
        run["strategy_version"] == 2
        and run["data_provider"] == "fixture"
        and run["config_hash"] == rep["strategy"]["config_hash"]
    )
    assert rep["metrics"]["total_trades"] >= 1 and "HISTORICAL SIMULATION" in rep["label"]
    trades = client.get(f"/api/backtests/{rid}/trades").json()
    assert trades["simulated"] is True and len(trades["trades"]) == rep["metrics"]["total_trades"]
    assert abs(sum(t["net_pnl"] for t in trades["trades"]) - rep["metrics"]["net_pnl"]) < 0.05
    assert rid in [r["run_id"] for r in client.get("/api/backtests").json()]
    assert client.get("/api/backtests/nope").status_code == 404


def test_backtest_request_validation_and_errors(client):
    bad = client.post(
        "/api/backtests",
        json={"strategy_id": "e2e-orb", "config": {"start_date": "2026-09-10", "end_date": "2026-09-01"}},
    )
    assert bad.status_code == 422
    assert (
        client.post(
            "/api/backtests",
            json={"strategy_id": "ghost", "config": {"start_date": "2026-09-01", "end_date": "2026-09-02"}},
        ).status_code
        == 404
    )
    pit = client.post(
        "/api/backtests",
        json={
            "strategy_id": "e2e-orb",
            "wait": True,
            "config": {
                "start_date": "2026-09-08",
                "end_date": "2026-09-09",
                "universe_mode": "point_in_time",
            },
        },
    ).json()
    assert client.get(f"/api/backtests/{pit['run_id']}").json()["status"] == "failed"


def test_paper_intent_errors_never_reach_a_broker(client):
    assert client.post("/api/paper-intents", json={"event_id": "ghost", "quantity": 1}).status_code == 404
    r = client.post(
        "/api/paper-intents",
        json={"symbol": "AAA", "direction": "long", "reference_price": 10, "risk_dollars": 0.001},
    )
    assert r.status_code == 422 and r.json()["detail"]["broker_submission"] is False
    assert client.post("/api/paper-intents", json={"quantity": 1}).status_code == 422
    manual = client.post(
        "/api/paper-intents",
        json={
            "symbol": "ORBX",
            "direction": "long",
            "reference_price": 27.0,
            "quantity": 10,
            "entry_model": "bid_ask_cross",
        },
    )
    assert manual.status_code == 201 and manual.json()["intent"]["event_id"] is None


def test_paper_intent_on_expired_alert_rejected(client):
    expired = [a for a in client.get("/api/alerts", params={"status": "expired"}).json()]
    if not expired:
        pytest.skip("no expired alert in this replay")
    r = client.post("/api/paper-intents", json={"event_id": expired[0]["event_id"], "quantity": 1})
    assert r.status_code == 422 and "expired" in r.json()["detail"]["detail"]


def test_alert_history_survives_service_view_and_suppressed_are_kept(client):
    all_alerts = client.get("/api/alerts", params={"limit": 1000}).json()
    statuses = {a["status"] for a in all_alerts}
    assert "suppressed" in statuses or "expired" in statuses
    sup = [a for a in all_alerts if a["status"] == "suppressed"]
    assert all(a["status_reason"] for a in sup)


def test_notifications_preferences_and_audit(client):
    t = client.post("/api/notifications/test").json()
    assert t["sent"][0]["status"] == "sent" and t["sent"][0]["kind"] == "test" and "No order" in t["note"]
    prefs = client.get("/api/preferences").json()
    prefs["browser_enabled"] = False
    assert client.put("/api/preferences", json=prefs).json()["browser_enabled"] is False
    t2 = client.post("/api/notifications/test").json()
    assert t2["sent"][0]["status"] == "skipped"
    prefs["browser_enabled"] = True
    client.put("/api/preferences", json=prefs)
    assert client.put("/api/preferences", json={"min_priority": "bogus", "max_retries": 99}).status_code in (
        200,
        422,
    )
    actions = {a["action"] for a in client.get("/api/audit").json()}
    assert {
        "strategy.create",
        "strategy.update",
        "alert.acknowledge",
        "paper_intent.create",
        "backtest.start",
        "notification.test",
    } <= actions


def test_symbol_activation_and_status_ws(client):
    assert client.post("/api/symbols/lowv/active", json={"active": False, "status": "delisted"}).json() == {
        "symbol": "LOWV",
        "active": False,
    }
    assert next(s for s in client.get("/api/symbols").json() if s["symbol"] == "LOWV")["status"] == "delisted"
    with client.websocket_connect("/ws/market-status") as ws:
        m = ws.receive_json()
        assert (
            m["type"] == "status"
            and m["paper_only"] is True
            and m["market_session"]
            and "PAPER ONLY" in m["labels"]
        )


def test_invalid_payloads_rejected(client):
    assert client.post("/api/strategies", json={"id": "!!", "name": "x"}).status_code == 422
    assert client.get("/api/alerts/ghost").status_code == 404
    assert client.post("/api/alerts/ghost/acknowledge").status_code == 404
    assert client.get("/api/top-lists/ghost").status_code == 404
    assert client.get("/api/alerts", params={"limit": 0}).status_code == 422


def test_ui_is_served(client):
    r = client.get("/ui/")
    assert r.status_code == 200 and "PAPER ONLY" in r.text
    assert client.get("/ui/app.js").status_code == 200 and client.get("/ui/style.css").status_code == 200
    assert client.get("/", follow_redirects=False).status_code in (302, 307)


def test_app_refuses_to_start_with_live_config(monkeypatch):
    from scanalert.config import PaperOnlyViolation

    monkeypatch.setenv("ALPACA_LIVE_KEY_ID", "AKLIVE")
    with pytest.raises(PaperOnlyViolation):
        create_app(Settings(database_url="sqlite://"))
    monkeypatch.delenv("ALPACA_LIVE_KEY_ID")
    with pytest.raises(ValueError):
        create_app(Settings(database_url="sqlite://", alpaca_data_ws_url="wss://api.tradestation.com/x"))  # type: ignore[arg-type]


def test_no_outbound_network_during_fixture_mode(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("outbound network connection attempted")

    app = create_app(Settings(database_url="sqlite://", fixture_autostart=False))
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket.socket, "connect_ex", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    with TestClient(app) as c:  # in-process ASGI transport: no sockets needed
        assert c.get("/health").status_code == 200
        assert c.post("/api/dev/replay/run", params={"until_minutes": 25}).status_code == 200
        assert (
            c.post(
                "/api/backtests",
                json={
                    "strategy_id": "opening-range-breakout",
                    "wait": True,
                    "config": {"start_date": "2026-09-14", "end_date": "2026-09-15"},
                },
            ).status_code
            == 202
        )
