"""Smoke test: start a real server (fresh temp DB, ephemeral port) and drive the paper-only flow over HTTP/WS.

Run with ``python -m scanalert smoke`` (or ``scripts/run-smoke-test.ps1``). Exit code 0 means every step passed.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

STEPS: list[tuple[str, bool, str]] = []


def step(name: str, ok: bool, detail: str = "") -> None:
    STEPS.append((name, ok, detail))
    print(f"[smoke] {'PASS' if ok else 'FAIL'}  {name}{('  - ' + detail) if detail else ''}")
    if not ok:
        raise AssertionError(name)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _guard_refuses_live() -> None:
    env = {**os.environ, "TRADING_MODE": "live"}
    r = subprocess.run(
        [sys.executable, "-m", "scanalert", "check-config"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    step(
        "guard refuses TRADING_MODE=live (exit code 2)",
        r.returncode == 2 and "REFUSING TO START" in r.stderr,
        r.stderr.strip().splitlines()[0] if r.stderr else "",
    )
    env = {**os.environ, "ALPACA_LIVE_SECRET_KEY": "x", "TRADING_MODE": "paper"}
    r = subprocess.run(
        [sys.executable, "-m", "scanalert", "check-config"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    step("guard refuses a live credential variable", r.returncode == 2)


def run_smoke() -> int:
    from websockets.sync.client import connect

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    tmp = tempfile.mkdtemp(prefix="scanalert-smoke-")
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{Path(tmp) / 'smoke.db'}",
        "FIXTURE_AUTOSTART": "false",
        "TRADING_MODE": "paper",
        "DATA_PROVIDER": "fixture",
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "scanalert.api:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _guard_refuses_live()
        with httpx.Client(base_url=base, timeout=120) as c:
            up = False
            for _ in range(80):
                try:
                    if c.get("/health").status_code == 200:
                        up = True
                        break
                except httpx.HTTPError:
                    time.sleep(0.25)
            step("server started and /health reachable", up)
            h = c.get("/health").json()
            step(
                "health reports paper-only mode",
                h["paper_only"] is True and h["trading_mode"] == "paper",
                json.dumps({k: h[k] for k in ("trading_mode", "market_session")}),
            )
            cfg = c.get("/api/config/public").json()
            step(
                "public config exposes no order endpoints and hides secrets",
                cfg["order_endpoints"] == [] and cfg["live_trading_supported"] is False,
            )
            step("UI served", "PAPER ONLY" in c.get("/ui/").text)

            good = {
                "id": "smoke-orb",
                "name": "Smoke ORB",
                "universe": ["ORBX"],
                "filters": [{"id": "px", "name": "px", "field": "last", "operator": "gte", "value": 5}],
                "alert_conditions": [
                    {
                        "id": "orb",
                        "name": "orb",
                        "event_type": "breakout",
                        "filters": [
                            {
                                "id": "o",
                                "name": "o",
                                "field": "orb_breakout_up",
                                "operator": "is_true",
                                "lookback": 15,
                            },
                            {"id": "r", "name": "r", "field": "rvol", "operator": "gte", "value": 1.5},
                        ],
                    }
                ],
            }
            bad = c.post(
                "/api/strategies/x/validate",
                json={
                    **good,
                    "filters": [{"id": "q", "name": "q", "field": "nope", "operator": "gt", "value": 1}],
                },
            ).json()
            step(
                "invalid strategy rejected with a clear error",
                bad["valid"] is False and "unknown field" in bad["errors"][0]["message"],
            )
            step(
                "valid strategy passes validation",
                c.post("/api/strategies/smoke-orb/validate", json=good).json()["valid"] is True,
            )
            step("strategy created (version 1)", c.post("/api/strategies", json=good).json()["version"] == 1)

            with connect(f"ws://127.0.0.1:{port}/ws/alerts") as ws:
                hello = json.loads(ws.recv(timeout=10))
                step("alerts WebSocket connected", hello["type"] == "hello" and hello["paper_only"] is True)
                r = c.post("/api/dev/replay/run", params={"until_minutes": 30}).json()
                step("fixture replay ran through the scanner", r["events"] > 500, f"{r['events']} events")
                alert = None
                for _ in range(60):
                    m = json.loads(ws.recv(timeout=10))
                    if m["type"] == "alert" and m["alert"]["strategy_id"] == "smoke-orb":
                        alert = m["alert"]
                        break
                step(
                    "live-style alert received over WebSocket",
                    alert is not None and alert["paper_only"] is True,
                    f"{alert['symbol']} @ {alert['trigger_price']}" if alert else "",
                )
            eid = alert["event_id"]  # type: ignore[index]
            step(
                "alert persisted and retrievable",
                c.get(f"/api/alerts/{eid}").json()["alert"]["status"] == "triggered",
            )
            tl = c.get("/api/top-lists/smoke-orb").json()
            step(
                "Top List snapshot available",
                "rows" in tl and tl["paper_only"] is True,
                f"{tl['qualified']} qualified",
            )
            pi = c.post("/api/paper-intents", json={"event_id": eid, "risk_dollars": 100})
            body = pi.json()
            step(
                "paper intent created (simulated, not submitted)",
                pi.status_code == 201
                and body["broker_submission"] is False
                and body["intent"]["submitted_to_broker"] is False
                and body["fills"][0]["status"] in ("filled", "partial"),
            )
            step(
                "alert acknowledged",
                c.post(f"/api/alerts/{eid}/acknowledge").json()["status"] == "acknowledged",
            )
            step(
                "test notification sent (non-transactional)",
                c.post("/api/notifications/test").json()["sent"][0]["status"] == "sent",
            )
            c.post("/api/dev/replay/run")
            bt = c.post(
                "/api/backtests",
                json={
                    "strategy_id": "smoke-orb",
                    "wait": True,
                    "config": {
                        "start_date": "2026-09-08",
                        "end_date": "2026-09-25",
                        "assumed_spread_bps": 6,
                        "exits": {
                            "profit_target": {"type": "percent", "value": 1.5},
                            "stop_loss": {"type": "percent", "value": 0.75},
                            "time_after_entry_minutes": 90,
                        },
                    },
                },
            ).json()
            run = c.get(f"/api/backtests/{bt['run_id']}").json()
            step(
                "backtest completed with report",
                run["status"] == "completed"
                and run["report"]["metrics"]["total_trades"] >= 1
                and "HISTORICAL SIMULATION" in run["report"]["label"],
                f"{run['report']['metrics']['total_trades']} trades, net {run['report']['metrics']['net_pnl']}",
            )
            trades = c.get(f"/api/backtests/{bt['run_id']}/trades").json()
            step("backtest trades are simulated", trades["simulated"] is True and len(trades["trades"]) > 0)
            step(
                "audit log recorded actions",
                {"strategy.create", "paper_intent.create", "backtest.start"}
                <= {a["action"] for a in c.get("/api/audit").json()},
            )
        print(f"[smoke] ALL {len(STEPS)} STEPS PASSED (paper-only, no broker contacted)")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[smoke] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
        if out and any(s[1] is False for s in STEPS) or (out and "Traceback" in out):
            print(out[-2000:], file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(run_smoke())
