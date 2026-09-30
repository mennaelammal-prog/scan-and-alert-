"""FastAPI application: REST + WebSocket endpoints and the static UI.

There is no order-routing endpoint in this application. ``POST /api/paper-intents`` records a simulated
intent and simulated fills only. Every response that could be mistaken for a trade is labelled.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from . import __version__
from .backtest import BacktestConfig
from .bootstrap import build_provider, build_store
from .config import Settings, assert_paper_only, load_settings
from .features import FEATURES
from .models import iso, parse_ts
from .notify import UserPreferences
from .paper import IntentRejected, PaperIntentRequest
from .providers.base import MarketDataProvider
from .service import Service
from .strategy import StrategySpec, validate_strategy

log = logging.getLogger("scanalert.api")
UI_DIR = Path(__file__).parent / "ui"

STRATEGY_EXAMPLE = {
    "id": "my-breakout",
    "name": "My breakout",
    "direction": "long",
    "filters": [
        {
            "id": "min-price",
            "name": "Price >= 5",
            "field": "last",
            "operator": "gte",
            "value": 5,
            "unit": "usd",
        },
        {
            "id": "liquid",
            "name": "Dollar volume >= 500k",
            "field": "dollar_volume",
            "operator": "gte",
            "value": 500000,
        },
    ],
    "alert_conditions": [
        {
            "id": "vwap-cross",
            "name": "Above VWAP with volume",
            "event_type": "vwap_break",
            "priority": "normal",
            "filters": [
                {"id": "rvol", "name": "RVOL >= 1.5", "field": "rvol", "operator": "gte", "value": 1.5},
                {
                    "id": "f",
                    "name": "formula",
                    "field": "formula",
                    "operator": "is_true",
                    "formula": "vwap_dist_pct > 0.3 and ret(5) > 0.2",
                },
            ],
        }
    ],
    "ranking": {"field": "rvol", "order": "desc"},
}


class BacktestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy_id: str
    strategy_version: int | None = None
    config: BacktestConfig
    universe: dict[str, Any] | None = None
    wait: bool = False


class ActiveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: bool
    status: str | None = None


def build_service(settings: Settings, provider: MarketDataProvider | None = None) -> Service:
    provider = provider or build_provider(settings)
    feed = {"fixture": "synthetic", "alpaca": settings.alpaca_data_feed}.get(provider.name, "")
    store = build_store(settings, provider.name, feed)
    return Service(settings, store, provider)


def create_app(
    settings: Settings | None = None,
    provider_factory: Callable[[Settings], MarketDataProvider] | None = None,
    autostart_replay: bool | None = None,
) -> FastAPI:
    """Application factory. ``load_settings`` runs the paper-only guard and refuses live configuration."""
    cfg = settings or load_settings()
    assert_paper_only(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        provider = provider_factory(cfg) if provider_factory else None
        svc = build_service(cfg, provider)
        app.state.service = svc
        auto = (
            autostart_replay
            if autostart_replay is not None
            else (cfg.data_provider != "fixture" or cfg.fixture_autostart)
        )
        await svc.start(autostart_replay=auto)
        try:
            yield
        finally:
            await svc.stop()

    app = FastAPI(
        title="scanalert - paper-only stock scanner & alert platform",
        version=__version__,
        description=(
            "PAPER ONLY / SIMULATION ONLY. Scanner events, alerts, paper intents, simulated fills and backtests. "
            "No endpoint places, routes, modifies or cancels a real order."
        ),
        lifespan=lifespan,
    )
    app.state.settings = cfg

    def svc_() -> Service:
        return app.state.service

    # ------------------------------------------------------------------ health/config
    @app.get("/health", tags=["system"])
    def health() -> dict[str, Any]:
        s = svc_().market_status()
        return {
            "status": "ok",
            "version": __version__,
            "paper_only": True,
            "trading_mode": cfg.trading_mode,
            "data_provider": s["provider"],
            "feed_state": s["feed_state"],
            "market_session": s["market_session"],
            "last_event_time": s["last_event_time"],
            "labels": s["labels"],
        }

    @app.get("/api/status", tags=["system"])
    def status() -> dict[str, Any]:
        return svc_().market_status()

    @app.get("/api/config/public", tags=["system"])
    def public_config() -> dict[str, Any]:
        return {
            **cfg.redacted(),
            "live_trading_supported": False,
            "order_endpoints": [],
            "safety": "Paper/simulation only. Startup fails closed if a live broker URL, credential or flag is configured.",
            "universe": svc_().universe(),
        }

    @app.get("/api/features", tags=["scanner"])
    def features() -> list[dict[str, Any]]:
        return [
            {
                "name": f.name,
                "unit": f.unit,
                "type": f.type,
                "description": f.description,
                "default_lookback": f.default_lookback,
                "max_lookback": f.max_lookback,
            }
            for f in FEATURES.values()
        ]

    @app.get("/api/symbols", tags=["scanner"])
    def symbols() -> list[dict[str, Any]]:
        return svc_().store.list_symbols()

    @app.post("/api/symbols/{symbol}/active", tags=["scanner"])
    def set_symbol_active(symbol: str, body: ActiveBody) -> dict[str, Any]:
        s = svc_()
        sym = symbol.upper()
        s.store.set_symbol_active(sym, body.active, body.status)
        if sym in s.states:
            s.states[sym].active = body.active
        s.store.audit("user", "symbol.active", "symbol", sym, str(body.active))
        return {"symbol": sym, "active": body.active}

    # ---------------------------------------------------------------- strategies
    @app.get("/api/strategies", tags=["strategies"])
    def list_strategies() -> list[dict[str, Any]]:
        return [
            s.model_dump(mode="json") | {"config_hash": s.config_snapshot()["config_hash"]}
            for s in svc_().store.list_strategies()
        ]

    @app.post("/api/strategies", status_code=201, tags=["strategies"])
    def create_strategy(spec: StrategySpec = Body(..., examples=[STRATEGY_EXAMPLE])) -> dict[str, Any]:
        s = svc_()
        try:
            created = s.store.create_strategy(spec)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        s.load_strategies()
        return created.model_dump(mode="json") | {"config_hash": created.config_snapshot()["config_hash"]}

    @app.get("/api/strategies/{strategy_id}", tags=["strategies"])
    def get_strategy(strategy_id: str, version: int | None = None) -> dict[str, Any]:
        spec = svc_().store.get_strategy(strategy_id, version)
        if spec is None:
            raise HTTPException(404, "strategy not found")
        return spec.model_dump(mode="json") | {"config_hash": spec.config_snapshot()["config_hash"]}

    @app.get("/api/strategies/{strategy_id}/versions", tags=["strategies"])
    def strategy_versions(strategy_id: str) -> list[dict[str, Any]]:
        v = svc_().store.strategy_versions(strategy_id)
        if not v:
            raise HTTPException(404, "strategy not found")
        return v

    @app.put("/api/strategies/{strategy_id}", tags=["strategies"])
    def update_strategy(
        strategy_id: str, spec: StrategySpec = Body(..., examples=[STRATEGY_EXAMPLE])
    ) -> dict[str, Any]:
        s = svc_()
        try:
            updated = s.store.update_strategy(strategy_id, spec)
        except KeyError as exc:
            raise HTTPException(404, "strategy not found") from exc
        s.load_strategies()
        return updated.model_dump(mode="json") | {
            "config_hash": updated.config_snapshot()["config_hash"],
            "note": "a new immutable version was created; existing alerts keep their original version",
        }

    @app.post("/api/strategies/{strategy_id}/validate", tags=["strategies"])
    def validate(
        strategy_id: str, body: dict[str, Any] | None = Body(None, examples=[STRATEGY_EXAMPLE])
    ) -> dict[str, Any]:
        payload = body
        if not payload:
            spec = svc_().store.get_strategy(strategy_id)
            if spec is None:
                raise HTTPException(404, "strategy not found")
            payload = spec.model_dump(mode="json")
        else:
            payload = {**payload, "id": payload.get("id", strategy_id)}
        return validate_strategy(payload)

    @app.post("/api/strategies/{strategy_id}/preview", tags=["strategies"])
    def preview(
        strategy_id: str, body: dict[str, Any] | None = Body(None), max_rows: int = Query(25, ge=1, le=200)
    ) -> dict[str, Any]:
        s = svc_()
        if body:
            result = validate_strategy({**body, "id": body.get("id", strategy_id)})
            if not result["valid"]:
                raise HTTPException(422, {"errors": result["errors"]})
            spec = StrategySpec.model_validate(result["spec"])
        else:
            spec = s.store.get_strategy(strategy_id)  # type: ignore[assignment]
            if spec is None:
                raise HTTPException(404, "strategy not found")
        return s.preview(spec, max_rows=max_rows)

    # -------------------------------------------------------------------- alerts
    @app.get("/api/alerts", tags=["alerts"])
    def list_alerts(
        status: str | None = None,
        symbol: str | None = None,
        strategy_id: str | None = None,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> list[dict[str, Any]]:
        s = svc_()
        live = {a.event_id: a for a in s.manager.recent(5000)}  # in-memory copies hold the freshest state
        rows = s.store.list_alerts(status, symbol.upper() if symbol else None, strategy_id, limit, offset)
        return [(live.get(a.event_id) or a).model_dump() for a in rows]

    @app.get("/api/alerts/{event_id}", tags=["alerts"])
    def get_alert(event_id: str) -> dict[str, Any]:
        s = svc_()
        a = s.manager.get(event_id) or s.store.get_alert(event_id)
        if a is None:
            raise HTTPException(404, "alert not found")
        intents = [i for i in s.store.list_paper_intents(500) if i["event_id"] == event_id]
        return {
            "alert": a.model_dump(),
            "deliveries": s.store.list_deliveries(event_id),
            "paper_intents": intents,
        }

    @app.post("/api/alerts/{event_id}/acknowledge", tags=["alerts"])
    def acknowledge(event_id: str) -> dict[str, Any]:
        try:
            return svc_().acknowledge(event_id).model_dump()
        except KeyError as exc:
            raise HTTPException(404, "alert not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/alerts/{event_id}/chart", tags=["alerts"])
    def alert_chart(
        event_id: str, before: int = Query(60, ge=5, le=390), after: int = Query(30, ge=0, le=120)
    ) -> dict[str, Any]:
        s = svc_()
        a = s.manager.get(event_id) or s.store.get_alert(event_id)
        if a is None:
            raise HTTPException(404, "alert not found")
        t0 = parse_ts(a.source_timestamp)
        rows = s.store.load_bars(
            a.symbol, iso(t0 - timedelta(minutes=before)), iso(t0 + timedelta(minutes=after))
        )
        st = s.states.get(a.symbol)
        if not rows and st:
            rows = [
                {
                    "bar_ts": iso(b.ts),
                    "open": b.open,
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                    "volume": b.volume,
                }
                for b in sorted(st.bars.values(), key=lambda b: b.ts)
                if t0 - timedelta(minutes=before) <= b.ts <= t0 + timedelta(minutes=after)
            ]
        return {
            "symbol": a.symbol,
            "trigger_ts": a.source_timestamp,
            "trigger_price": a.trigger_price,
            "bars": [{k: r[k] for k in ("bar_ts", "open", "high", "low", "close", "volume")} for r in rows],
            "external_chart": f"https://www.tradingview.com/chart/?symbol={a.symbol}",
            "note": "Local chart from stored 1-minute bars. External link is a plain chart link (no data or orders sent).",
        }

    # ----------------------------------------------------------------- top lists
    @app.get("/api/top-lists/{strategy_id}", tags=["top-lists"])
    def top_list(
        strategy_id: str, refresh: bool = True, max_rows: int | None = Query(None, ge=1, le=1000)
    ) -> dict[str, Any]:
        s = svc_()
        if strategy_id not in s.strategies:
            raise HTTPException(404, "strategy not found or not loaded")
        snap = s.top_lists.get(strategy_id)
        if refresh or snap is None or max_rows:
            snap = s.compute_top_list(strategy_id, max_rows)
        out = snap.as_dict()
        out["refresh_seconds"] = s.strategies[strategy_id].top_list.refresh_seconds
        return out

    # ----------------------------------------------------------------- backtests
    @app.post("/api/backtests", status_code=202, tags=["backtests"])
    async def start_backtest(req: BacktestRequest) -> dict[str, Any]:
        s = svc_()
        spec = s.store.get_strategy(req.strategy_id, req.strategy_version)
        if spec is None:
            raise HTTPException(404, "strategy not found")
        try:
            run_id = await s.start_backtest(spec, req.config, req.universe, wait=req.wait)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        run = s.store.get_backtest_run(run_id)
        return {
            "run_id": run_id,
            "status": run["status"] if run else "running",
            "label": "BACKTEST - HISTORICAL SIMULATION",
        }

    @app.get("/api/backtests", tags=["backtests"])
    def list_backtests(limit: int = Query(50, ge=1, le=200)) -> list[dict[str, Any]]:
        return svc_().store.list_backtest_runs(limit)

    @app.get("/api/backtests/{run_id}", tags=["backtests"])
    def get_backtest(run_id: str) -> dict[str, Any]:
        run = svc_().store.get_backtest_run(run_id)
        if run is None:
            raise HTTPException(404, "backtest run not found")
        return run

    @app.get("/api/backtests/{run_id}/trades", tags=["backtests"])
    def backtest_trades(
        run_id: str, limit: int = Query(1000, ge=1, le=10000), offset: int = Query(0, ge=0)
    ) -> dict[str, Any]:
        s = svc_()
        if s.store.get_backtest_run(run_id) is None:
            raise HTTPException(404, "backtest run not found")
        return {
            "run_id": run_id,
            "simulated": True,
            "trades": s.store.list_backtest_trades(run_id, limit, offset),
        }

    # -------------------------------------------------------------- paper trading
    @app.post("/api/paper-intents", status_code=201, tags=["paper"])
    def create_paper_intent(
        req: PaperIntentRequest = Body(
            ...,
            examples=[
                {
                    "event_id": "evt_...",
                    "risk_dollars": 100,
                    "entry_model": "spread_plus_slippage",
                    "stop": {"type": "percent", "value": 1.0},
                    "target": {"type": "r_multiple", "value": 2.0},
                }
            ],
        ),
    ) -> dict[str, Any]:
        """Creates a SIMULATED intent (never submitted to any broker) and, optionally, simulated fills."""
        try:
            return svc_().create_paper_intent(req)
        except KeyError as exc:
            raise HTTPException(404, "alert not found") from exc
        except IntentRejected as exc:
            raise HTTPException(
                422, {"error": "intent_rejected", "detail": str(exc), "broker_submission": False}
            ) from exc

    @app.get("/api/paper-intents", tags=["paper"])
    def list_paper_intents(limit: int = Query(100, ge=1, le=500)) -> list[dict[str, Any]]:
        return svc_().store.list_paper_intents(limit)

    @app.get("/api/paper-fills", tags=["paper"])
    def list_paper_fills(intent_id: str | None = None) -> list[dict[str, Any]]:
        return svc_().store.list_paper_fills(intent_id)

    @app.get("/api/paper-positions", tags=["paper"])
    def paper_positions() -> dict[str, Any]:
        return {
            "simulated": True,
            "label": "SIMULATED POSITIONS - NOT BROKER POSITIONS",
            "positions": svc_().list_paper_positions(),
        }

    # ------------------------------------------------------ notifications & admin
    @app.post("/api/notifications/test", tags=["notifications"])
    async def test_notification() -> dict[str, Any]:
        return await svc_().send_test_notification()

    @app.get("/api/preferences", tags=["notifications"])
    def get_preferences() -> dict[str, Any]:
        return svc_().prefs.prefs.model_dump()

    @app.put("/api/preferences", tags=["notifications"])
    def put_preferences(prefs: UserPreferences) -> dict[str, Any]:
        return svc_().prefs.update(prefs).model_dump()

    @app.get("/api/deliveries", tags=["notifications"])
    def deliveries(limit: int = Query(200, ge=1, le=1000)) -> list[dict[str, Any]]:
        return svc_().store.list_deliveries(limit=limit)

    @app.get("/api/audit", tags=["system"])
    def audit(limit: int = Query(200, ge=1, le=1000)) -> list[dict[str, Any]]:
        return svc_().store.list_audit(limit)

    @app.post("/api/dev/replay/run", tags=["dev"])
    async def dev_replay_run(
        until_minutes: float | None = Query(
            None, ge=0, le=390, description="pause after this many minutes since the open"
        ),
    ) -> dict[str, Any]:
        """Fixture provider only: consume (part of) the replay synchronously (deterministic demos/tests)."""
        s = svc_()
        if s.provider.name != "fixture":
            raise HTTPException(409, "replay control is available for the fixture provider only")
        if s._ingest_task and not s._ingest_task.done():
            s._ingest_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await s._ingest_task
        until = None
        if until_minutes is not None:
            live_day = getattr(s.provider, "live_days", [None])[0]
            if live_day is not None:
                until = s.calendar.open_dt(live_day) + timedelta(minutes=until_minutes)
        n = await s.replay_all(until)
        return {
            "events": n,
            "alerts": len(s.manager.recent(5000)),
            "replay_complete": s.replay_complete,
            "clock": iso(s.clock()),
        }

    @app.post("/api/dev/replay/restart", tags=["dev"])
    async def dev_replay_restart() -> dict[str, Any]:
        """Fixture provider only: rebuild in-memory state and replay the live day from the start."""
        s = svc_()
        if s.provider.name != "fixture":
            raise HTTPException(409, "replay control is available for the fixture provider only")
        await s.stop()
        new = build_service(cfg, provider_factory(cfg) if provider_factory else None)
        app.state.service = new
        await new.start(autostart_replay=True)
        return {"restarted": True}

    # ----------------------------------------------------------------- websockets
    @app.websocket("/ws/alerts")
    async def ws_alerts(ws: WebSocket) -> None:
        await ws.accept()
        s = svc_()
        q = s.hub.subscribe("alerts")
        await ws.send_json(
            {"type": "hello", "paper_only": True, "recent": [a.model_dump() for a in s.manager.recent(50)]}
        )

        async def pump() -> None:
            while True:
                await ws.send_text(await q.get())

        task = asyncio.create_task(pump())
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                if msg.get("type") == "ack":
                    try:
                        a = s.acknowledge(str(msg.get("event_id")), "websocket")
                        await ws.send_json({"type": "ack_ok", "alert": a.model_dump()})
                    except (KeyError, ValueError) as exc:
                        await ws.send_json({"type": "error", "detail": str(exc)})
                elif msg.get("type") == "ping":
                    await ws.send_json({"type": "pong", "ts": iso(s.clock())})
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            task.cancel()
            s.hub.unsubscribe("alerts", q)

    @app.websocket("/ws/market-status")
    async def ws_status(ws: WebSocket) -> None:
        await ws.accept()
        s = svc_()
        q = s.hub.subscribe("status")
        await ws.send_json({"type": "status", **s.market_status()})

        async def pump() -> None:
            while True:
                await ws.send_text(await q.get())

        task = asyncio.create_task(pump())
        try:
            while True:
                await ws.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            task.cancel()
            s.hub.unsubscribe("status", q)

    # ------------------------------------------------------------------------ UI
    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/ui/")

    if UI_DIR.is_dir():
        app.mount("/ui", StaticFiles(directory=str(UI_DIR), html=True), name="ui")
    return app
