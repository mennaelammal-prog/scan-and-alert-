"""Runtime wiring: provider events -> market state -> scanners -> alerts -> notifications.

One ``Service`` owns the in-memory market state and the paper book. It is created by the API lifespan and
by tests. Nothing here submits an order: paper intents produce simulated fills only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from .alerts import AlertEvent, AlertManager
from .backtest import BacktestConfig, PointInTimeUniverse, StaticUniverse, new_run_id, run_backtest
from .calendar import NyseCalendar, et_date
from .config import Settings
from .db import Store
from .features import FeatureContext, SymbolState
from .models import (
    Bar,
    HistoricalBarsRequest,
    MarketEvent,
    ProviderEvent,
    Quote,
    SessionEvent,
    Trade,
    iso,
    utcnow,
)
from .notify import (
    EmailChannel,
    Hub,
    NotificationRouter,
    PreferenceStore,
    WebhookChannel,
    WebSocketChannel,
)
from .paper import (
    FillConfig,
    IntentRejected,
    MarketSnapshot,
    PaperIntent,
    PaperIntentRequest,
    PaperPosition,
    build_intent,
    resolve_stop_target,
    simulate_fill,
)
from .providers.base import MarketDataProvider
from .scanner import EventScanner, SnapshotScanner, TopListSnapshot
from .strategy import StrategySpec, momentum_gap, opening_range_breakout

log = logging.getLogger("scanalert.service")
HISTORY_DAYS = 24


class Service:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        provider: MarketDataProvider,
        calendar: NyseCalendar | None = None,
        eval_on_ticks: bool | None = None,
    ):
        self.settings, self.store, self.provider = settings, store, provider
        self.calendar = calendar or NyseCalendar(settings.enable_premarket, settings.enable_postmarket)
        self.hub = Hub()
        self.prefs = PreferenceStore(store.load_preferences(), persist=store.save_preferences)
        self.states: dict[str, SymbolState] = {}
        self.strategies: dict[str, StrategySpec] = {}
        self.scanner = EventScanner(self.calendar)
        self.snapshotter = SnapshotScanner(self.calendar, settings.stale_feed_seconds)
        self.manager = AlertManager(
            store, self.clock, settings.stale_feed_seconds, provider.name, self.feed_name
        )
        self.router = NotificationRouter(
            self._build_channels(),
            sink=store.save_delivery,
            clock=self.clock,
            delayed_ms=settings.delayed_delivery_ms,
        )
        self.manager.add_listener(self._on_alert_change)
        self.top_lists: dict[str, TopListSnapshot] = {}
        self.provider_events: deque[dict[str, Any]] = deque(maxlen=200)
        self.eval_on_ticks = (provider.name != "fixture") if eval_on_ticks is None else eval_on_ticks
        self._last_eval: dict[str, datetime] = {}
        self._bar_buf: list[Bar] = []
        self._raw_buf: list[MarketEvent] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._ingest_task: asyncio.Task[None] | None = None
        self._bg: list[asyncio.Task[None]] = []
        self.events_processed = 0
        self.replay_complete = False
        self.started = False
        self.paper_positions: dict[str, PaperPosition] = {}
        self._pending_intents: dict[str, PaperIntent] = {}
        self.fill_cfg = FillConfig()
        self.backtest_tasks: dict[str, asyncio.Task[None]] = {}

    # ------------------------------------------------------------------ basics
    @property
    def feed_name(self) -> str:
        return {"fixture": "synthetic", "alpaca": self.settings.alpaca_data_feed}.get(self.provider.name, "")

    def clock(self) -> datetime:
        sim = getattr(self.provider, "sim_now", None)
        if self.provider.name == "fixture":
            if sim is not None:
                return sim
            end = getattr(self.provider, "history_end", None)
            return end() if end else utcnow()
        return utcnow()

    def _build_channels(self) -> dict[str, Any]:
        ch: dict[str, Any] = {"browser": WebSocketChannel(self.hub)}
        if self.settings.webhook_enabled and self.settings.webhook_url:
            ch["webhook"] = WebhookChannel(self.settings.webhook_url)
        if self.settings.email_enabled:
            ch["email"] = EmailChannel()
        return ch

    def universe(self) -> list[str]:
        return self.settings.universe_symbols

    def state(self, symbol: str) -> SymbolState:
        st = self.states.get(symbol)
        if st is None:
            st = self.states[symbol] = SymbolState(symbol, self.calendar)
        return st

    def load_strategies(self) -> None:
        self.strategies = {s.id: s for s in self.store.list_strategies()}

    def ensure_default_strategies(self) -> None:
        if not self.store.list_strategies():
            for factory in (opening_range_breakout, momentum_gap):
                self.store.create_strategy(factory())
        self.load_strategies()

    # -------------------------------------------------------------- lifecycle
    async def start(self, autostart_replay: bool = True) -> None:
        if self.started:
            return
        syms = self.universe()
        self.store.upsert_symbols(syms)
        self.ensure_default_strategies()
        await self.provider.start()
        await self._seed_history(syms)
        await self.provider.subscribe_bars(syms, "1Min")
        await self.provider.subscribe_quotes(syms)
        await self.provider.subscribe_trades(syms)
        self.started = True
        if autostart_replay:
            self._ingest_task = asyncio.create_task(self._ingest_loop(), name="ingest")
        self._bg = [asyncio.create_task(self._housekeeping(), name="housekeeping")]

    async def stop(self) -> None:
        for t in [self._ingest_task, *self._bg, *self._tasks, *self.backtest_tasks.values()]:
            if t and not t.done():
                t.cancel()
        for t in [self._ingest_task, *self._bg, *self._tasks]:
            if t:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
        await self.provider.stop()
        await self._flush()
        self.started = False

    async def _seed_history(self, syms: list[str]) -> None:
        now = self.clock()
        start = now - timedelta(days=HISTORY_DAYS * 2)
        bars = await self.provider.historical_bars(
            HistoricalBarsRequest(syms, start, now, "1Min", feed=self.feed_name)
        )
        for s in syms:
            self.state(s)
        for b in bars:
            self.states.setdefault(b.symbol, SymbolState(b.symbol, self.calendar)).on_bar(b)
        self.store.record_sessions(self.calendar, self.calendar.trading_days(et_date(start), et_date(now)))
        log.info("seeded %d historical bars for %d symbols", len(bars), len(syms))

    async def _ingest_loop(self) -> None:
        try:
            async for ev in self.provider.events():
                self.handle_event(ev)
                if len(self._bar_buf) >= 500 or len(self._raw_buf) >= 1000:
                    await self._flush()
            self.replay_complete = self.provider.name == "fixture"
            await self._flush()
            self._publish_status()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("ingest loop crashed")

    async def replay_all(self, until: datetime | None = None) -> int:
        """Consume the provider stream in the foreground (fixture replay); returns events handled.

        With ``until`` the replay pauses after the first event at/after that simulated time; calling again
        resumes from there.
        """
        n0 = self.events_processed
        finished = True
        async for ev in self.provider.events():
            self.handle_event(ev)
            ts = getattr(ev, "ts", None)
            if until is not None and ts is not None and not isinstance(ev, ProviderEvent) and ts >= until:
                finished = False
                break
        if finished:
            self.replay_complete = self.provider.name == "fixture"
        await self._flush()
        await self.drain()
        return self.events_processed - n0

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _flush(self) -> None:
        bars, raw = self._bar_buf, self._raw_buf
        self._bar_buf, self._raw_buf = [], []
        if bars:
            await asyncio.to_thread(self.store.save_bars, bars)
        if raw:
            await asyncio.to_thread(self.store.save_raw_events, raw)

    async def _housekeeping(self) -> None:
        last_top = 0.0
        while True:
            await asyncio.sleep(1.0)
            loop_t = asyncio.get_running_loop().time()
            self._publish_status()
            if loop_t - last_top >= self.settings.top_list_refresh_seconds:
                last_top = loop_t
                for sid in list(self.strategies):
                    if self.strategies[sid].enabled and self.states:
                        snap = self.compute_top_list(sid)
                        self.hub.publish(
                            "alerts", {"type": "top_list", "strategy_id": sid, "snapshot": snap.as_dict()}
                        )

    # ------------------------------------------------------------- event intake
    def handle_event(self, ev: MarketEvent) -> None:
        self.events_processed += 1
        if isinstance(ev, Bar):
            st = self.state(ev.symbol)
            if st.on_bar(ev):
                self._bar_buf.append(ev)
                if self.settings.persist_raw_events:
                    self._raw_buf.append(ev)
                as_of = ev.ts + timedelta(minutes=1)
                self._paper_on_bar(ev)
                self._evaluate(ev.symbol, as_of)
                self._advance_alerts(as_of)
        elif isinstance(ev, Quote):
            self.state(ev.symbol).on_quote(ev)
            self._maybe_eval_tick(ev.symbol, ev.ts)
        elif isinstance(ev, Trade):
            self.state(ev.symbol).on_trade(ev)
            self._paper_on_trade(ev)
            self._maybe_eval_tick(ev.symbol, ev.ts)
        elif isinstance(ev, SessionEvent):
            if ev.symbol and ev.kind_ in ("halt", "resume"):
                self.state(ev.symbol).halted = ev.kind_ == "halt"
            if self.settings.persist_raw_events:
                self._raw_buf.append(ev)
            self.provider_events.append(
                {"kind": ev.kind_, "symbol": ev.symbol, "ts": iso(ev.ts), "detail": ev.detail}
            )
            self._publish_status()
        elif isinstance(ev, ProviderEvent):
            self.provider_events.append({"kind": ev.kind_, "ts": iso(ev.ts), "detail": ev.detail})
            if self.settings.persist_raw_events:
                self._raw_buf.append(ev)
            self._publish_status()

    def _maybe_eval_tick(self, symbol: str, ts: datetime) -> None:
        if not self.eval_on_ticks:
            return
        last = self._last_eval.get(symbol)
        if last is not None and (ts - last).total_seconds() * 1000 < self.settings.eval_min_interval_ms:
            return
        self._last_eval[symbol] = ts
        self._evaluate(symbol, ts)

    def _evaluate(self, symbol: str, as_of: datetime) -> None:
        st = self.states.get(symbol)
        if st is None:
            return
        for strat in list(self.strategies.values()):
            if not strat.enabled:
                continue
            uni = set(strat.universe) if strat.universe else None
            for sig in self.scanner.evaluate(strat, st, as_of, uni):
                alert = self.manager.submit(sig)
                if alert.status == "triggered":
                    self._spawn(self._notify(alert, self._delivery_time()))

    def _advance_alerts(self, now: datetime) -> None:
        def still_true(a: AlertEvent) -> bool | None:
            strat, st = self.strategies.get(a.strategy_id), self.states.get(a.symbol)
            if strat is None or st is None:
                return None
            return self.scanner.still_true(strat, a.condition_id, st, now)

        for a in self.manager.advance(now, still_true):
            if a.status == "triggered":
                self._spawn(self._notify(a, self._delivery_time()))

    def _spawn(self, coro: Any) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def _delivery_time(self) -> datetime | None:
        """Fixture replay uses a simulated clock that races ahead of async tasks: pin delivery to the alert moment."""
        return self.clock() if self.provider.name == "fixture" else None

    async def _notify(self, alert: AlertEvent, at: datetime | None = None) -> None:
        try:
            await self.router.dispatch(alert, self.prefs.prefs, at=at)
            await asyncio.to_thread(self.store.save_alert, alert)
        except Exception:  # noqa: BLE001
            log.exception("notification failed for %s", alert.event_id)

    def _on_alert_change(self, alert: AlertEvent) -> None:
        if alert.status != "triggered" or alert.acknowledged_at:
            self.hub.publish(
                "alerts", {"type": "alert_update", "alert": alert.model_dump(), "paper_only": True}
            )

    # -------------------------------------------------------------- read models
    def market_status(self) -> dict[str, Any]:
        now = self.clock()
        h = self.provider_health_sync()
        session = self.calendar.session_at(now)
        d = et_date(now)
        wall = utcnow()
        last_ing = h.get("last_ingest_ts")
        feed_state = "stopped"
        if self.started:
            if self.provider.name == "fixture":
                feed_state = "replay_complete" if self.replay_complete else "replaying"
            elif not h.get("connected"):
                feed_state = "disconnected"
            else:
                age = (
                    (wall - datetime.fromisoformat(str(last_ing).replace("Z", "+00:00"))).total_seconds()
                    if last_ing
                    else None
                )
                feed_state = (
                    "stale"
                    if (session == "regular" and (age is None or age > self.settings.stale_feed_seconds))
                    else "live"
                )
        recent = self.manager.recent(5000)
        return {
            "paper_only": True,
            "trading_mode": self.settings.trading_mode,
            "labels": ["PAPER ONLY", "SIMULATED"] + (["STALE FEED"] if feed_state == "stale" else []),
            "market_session": session,
            "is_trading_day": self.calendar.is_trading_day(d),
            "early_close": self.calendar.is_early_close(d),
            "clock": iso(now),
            "provider": h,
            "feed_state": feed_state,
            "last_event_time": h.get("last_event_ts"),
            "symbols": len(self.states),
            "alerts_total": len(recent),
            "alerts_triggered": sum(1 for a in recent if a.status == "triggered"),
            "events_processed": self.events_processed,
            "strategies_enabled": sum(1 for s in self.strategies.values() if s.enabled),
            "recent_provider_events": list(self.provider_events)[-10:],
        }

    def provider_health_sync(self) -> dict[str, Any]:
        h = getattr(self.provider, "_health", None) or getattr(
            getattr(self.provider, "stream", None), "health", None
        )
        return h.as_dict() if h is not None else {"provider": self.provider.name, "connected": False}

    def _publish_status(self) -> None:
        if self.hub.subscriber_count("status"):
            self.hub.publish("status", {"type": "status", **self.market_status()})

    def compute_top_list(self, strategy_id: str, max_rows: int | None = None) -> TopListSnapshot:
        strat = self.strategies.get(strategy_id)
        if strat is None:
            raise KeyError(strategy_id)
        uni = set(strat.universe) if strat.universe else None
        snap = self.snapshotter.run(
            strat,
            self.states,
            self.clock(),
            max_rows or self.settings.top_list_max_rows,
            generated_at=self.clock(),
            universe=uni,
        )
        self.top_lists[strategy_id] = snap
        return snap

    def preview(
        self, spec: StrategySpec, symbols: list[str] | None = None, max_rows: int = 25
    ) -> dict[str, Any]:
        """Evaluate a (possibly unsaved) strategy against the current state; no alerts are created."""
        as_of = self.clock()
        uni = set(symbols) if symbols else (set(spec.universe) if spec.universe else None)
        snap = self.snapshotter.run(spec, self.states, as_of, max_rows, universe=uni)
        return {
            "preview": True,
            "note": "Preview against current/fixture data. No alerts created, nothing submitted.",
            "top_list": snap.as_dict(),
        }

    # ------------------------------------------------------------- alert actions
    def acknowledge(self, event_id: str, by: str = "user") -> AlertEvent:
        a = self.manager.get(event_id)
        if a is None:  # historical alert (e.g. after restart): load, mutate, persist
            stored = self.store.get_alert(event_id)
            if stored is None:
                raise KeyError(event_id)
            self.manager.alerts[event_id] = stored
            self.manager._order.append(event_id)
            if stored.status not in ("acknowledged", "invalidated", "expired", "suppressed"):
                self.manager._open.add(event_id)
        out = self.manager.acknowledge(event_id, by)
        self.store.audit(by, "alert.acknowledge", "alert", event_id)
        return out

    async def send_test_notification(self) -> dict[str, Any]:
        """Non-transactional test notification: goes through the router, never touches orders."""
        now = self.clock()
        alert = AlertEvent(
            event_id="test_" + iso(now or utcnow())[-9:-1].replace(":", "").replace(".", ""),  # type: ignore[index]
            symbol="TEST",
            strategy_id="notification-test",
            strategy_version=0,
            condition_id="test",
            direction="long",
            event_type="test",
            source_timestamp=iso(now) or "",
            detected_timestamp=iso(now) or "",
            session="regular",
            trigger_price=None,
            status="triggered",
            priority="normal",
            label="TEST NOTIFICATION - NOT A SIGNAL - PAPER ONLY",
        )
        deliveries = await self.router.dispatch(alert, self.prefs.prefs, kind="test")
        self.store.audit("user", "notification.test", "notification", alert.event_id)
        return {
            "sent": [d.as_dict() for d in deliveries],
            "note": "Test notification only. No order is created or sent.",
        }

    # -------------------------------------------------------------- paper trading
    def _snapshot(self, symbol: str) -> MarketSnapshot:
        st = self.states.get(symbol)
        now = self.clock()
        if st is None:
            return MarketSnapshot(as_of=now, data_age_seconds=1e9)
        ctx = FeatureContext(st, now, "regular")
        age = (now - st.last_event_ts).total_seconds() if st.last_event_ts else 1e9
        avg_vol = ctx.get("avg_volume", 20)
        return MarketSnapshot(
            as_of=now,
            bid=st.bid,
            ask=st.ask,
            bid_size=st.bid_size,
            ask_size=st.ask_size,
            last=st.last_price(now),
            next_bar_volume=float(avg_vol) if isinstance(avg_vol, float) else 0.0,
            data_age_seconds=max(0.0, age),
        )

    def create_paper_intent(self, req: PaperIntentRequest) -> dict[str, Any]:
        alert = None
        if req.event_id:
            alert = self.manager.get(req.event_id) or self.store.get_alert(req.event_id)
            if alert is None:
                raise KeyError(req.event_id)
        intent = build_intent(req, alert, self.clock())
        self.store.save_paper_intent(intent.as_row())
        self.store.audit(
            "user",
            "paper_intent.create",
            "paper_intent",
            intent.intent_id,
            f"{intent.symbol} {intent.direction} x{intent.quantity} (simulated)",
        )
        fills: list[dict[str, Any]] = []
        if req.simulate_fill:
            if intent.entry_model in ("next_trade", "next_bar_open"):
                self._pending_intents[intent.intent_id] = intent  # fills on the next print / bar
            else:
                fills.append(self._fill_entry(intent, self._snapshot(intent.symbol)))
        return {
            "intent": intent.as_dict(),
            "fills": fills,
            "broker_submission": False,
            "simulated": True,
            "label": intent.label,
        }

    def _fill_entry(self, intent: PaperIntent, snap: MarketSnapshot) -> dict[str, Any]:
        side: Literal["buy", "sell"] = "buy" if intent.direction == "long" else "sell"
        cfg = FillConfig(**{**self.fill_cfg.__dict__, "slippage_bps": intent.est_slippage_bps})
        fill = simulate_fill(
            intent.intent_id, intent.symbol, side, intent.quantity, intent.entry_model, snap, cfg
        )
        self.store.save_paper_fill(fill.as_row())
        intent.status = {"filled": "filled", "partial": "partially_filled", "rejected": "rejected"}[
            fill.status
        ]
        self.store.save_paper_intent(intent.as_row())
        if fill.price is not None and fill.quantity > 0:
            entry_dt = datetime.fromisoformat(fill.fill_ts.replace("Z", "+00:00"))
            self.paper_positions[intent.intent_id] = PaperPosition(
                intent.symbol,
                intent.direction,
                fill.quantity,
                fill.price,
                intent.intent_id,
                fill.fill_ts,
                intent.stop_price,
                intent.target_price,
                iso(entry_dt + timedelta(minutes=intent.time_exit_minutes))
                if intent.time_exit_minutes
                else None,
            )
        return fill.as_row()

    def _paper_on_trade(self, t: Trade) -> None:
        for iid, intent in list(self._pending_intents.items()):
            if intent.symbol != t.symbol or intent.entry_model != "next_trade":
                continue
            if t.ts > datetime.fromisoformat(intent.expires_at.replace("Z", "+00:00")):
                self._expire_intent(iid)
                continue
            snap = MarketSnapshot(
                as_of=t.ts,
                next_trade_price=t.price,
                next_trade_size=t.size,
                last=t.price,
                data_age_seconds=0.0,
            )
            self._fill_entry(intent, snap)
            del self._pending_intents[iid]

    def _expire_intent(self, iid: str) -> None:
        intent = self._pending_intents.pop(iid, None)
        if intent:
            intent.status = "expired"
            self.store.save_paper_intent(intent.as_row())

    def _paper_on_bar(self, bar: Bar) -> None:
        for iid, intent in list(self._pending_intents.items()):
            if intent.symbol != bar.symbol or intent.entry_model != "next_bar_open":
                continue
            if bar.ts > datetime.fromisoformat(intent.expires_at.replace("Z", "+00:00")):
                self._expire_intent(iid)
                continue
            snap = MarketSnapshot(
                as_of=bar.ts,
                next_bar_open=bar.open,
                next_bar_volume=bar.volume,
                last=bar.open,
                data_age_seconds=0.0,
            )
            if bar.ts >= datetime.fromisoformat(intent.created_at.replace("Z", "+00:00")):
                self._fill_entry(intent, snap)
                del self._pending_intents[iid]
        for iid, pos in list(self.paper_positions.items()):
            if pos.status != "open" or pos.symbol != bar.symbol:
                continue
            opened = datetime.fromisoformat(pos.opened_at.replace("Z", "+00:00"))
            if bar.ts + timedelta(minutes=1) <= opened:
                continue
            res = resolve_stop_target(pos.direction, pos.stop_price, pos.target_price, bar, "stop_first")
            reason, price = (None, None)
            if isinstance(res, tuple):
                reason, price = ("stop_loss" if res[0] == "stop" else "profit_target"), res[1]
            elif pos.time_exit_at and bar.ts + timedelta(minutes=1) >= datetime.fromisoformat(
                pos.time_exit_at.replace("Z", "+00:00")
            ):
                reason, price = "time_exit", bar.close
            if reason and price is not None:
                self._close_paper_position(iid, pos, price, reason, bar.ts + timedelta(minutes=1))

    def _close_paper_position(
        self, iid: str, pos: PaperPosition, price: float, reason: str, ts: datetime
    ) -> None:
        side = "sell" if pos.direction == "long" else "buy"
        slip = price * self.fill_cfg.slippage_bps / 10_000 if reason != "profit_target" else 0.0
        px = price - slip if side == "sell" else price + slip
        fill = {
            "fill_id": "pf_exit_" + iid[3:],
            "intent_id": iid,
            "symbol": pos.symbol,
            "side": side,
            "quantity": pos.quantity,
            "price": round(px, 4),
            "fill_ts": iso(ts),
            "model": f"exit:{reason}",
            "partial": False,
            "status": "filled",
            "reject_reason": None,
            "detail": {"note": "SIMULATED exit - never sent to a broker", "reason": reason},
        }
        self.store.save_paper_fill(fill)
        sgn = 1 if pos.direction == "long" else -1
        pos.status, pos.exit_price, pos.exit_reason, pos.closed_at = "closed", round(px, 4), reason, iso(ts)
        pos.realized_pnl = round(sgn * (px - pos.avg_entry) * pos.quantity, 2)

    def list_paper_positions(self) -> list[dict[str, Any]]:
        out = []
        for pos in self.paper_positions.values():
            st = self.states.get(pos.symbol)
            mark = st.last_price(self.clock()) if st else None
            out.append(pos.as_dict(mark))
        return sorted(out, key=lambda d: d["opened_at"], reverse=True)

    # -------------------------------------------------------------- backtests
    async def load_backtest_bars(
        self, symbols: list[str], start: date, end: date, warmup_days: int
    ) -> dict[str, list[Bar]]:
        s = datetime.combine(start - timedelta(days=warmup_days * 2 + 4), datetime.min.time(), UTC)
        e = datetime.combine(end + timedelta(days=2), datetime.min.time(), UTC)
        bars = await self.provider.historical_bars(
            HistoricalBarsRequest(symbols, s, e, "1Min", feed=self.feed_name)
        )
        out: dict[str, list[Bar]] = {sym: [] for sym in symbols}
        for b in bars:
            out.setdefault(b.symbol, []).append(b)
        return out

    async def start_backtest(
        self,
        spec: StrategySpec,
        cfg: BacktestConfig,
        universe: dict[str, Any] | None = None,
        wait: bool = False,
    ) -> str:
        run_id = new_run_id()
        symbols = cfg.symbols or spec.universe or self.universe()
        params = cfg.model_dump(mode="json") | {"universe": symbols, "universe_mode": cfg.universe_mode}
        self.store.create_backtest_run(run_id, spec, params, self.provider.name, self.feed_name)
        self.store.audit("user", "backtest.start", "backtest", run_id, f"{spec.id} v{spec.version}")

        async def job() -> None:
            try:
                bars = await self.load_backtest_bars(symbols, cfg.start_date, cfg.end_date, cfg.warmup_days)
                uni: Any = StaticUniverse(symbols)
                if cfg.universe_mode == "point_in_time":
                    snaps = {
                        date.fromisoformat(k): v for k, v in (universe or {}).get("snapshots", {}).items()
                    }
                    if not snaps:
                        raise ValueError("point_in_time mode requires universe.snapshots {date: [symbols]}")
                    dl = {k: date.fromisoformat(v) for k, v in (universe or {}).get("delisted", {}).items()}
                    uni = PointInTimeUniverse(snaps, dl)
                result = await asyncio.to_thread(
                    run_backtest, spec, cfg, bars, self.calendar, uni, self.provider.name, self.feed_name
                )
                self.store.save_backtest_trades(run_id, result.trades)
                self.store.finish_backtest_run(run_id, result.report)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("backtest %s failed", run_id)
                self.store.finish_backtest_run(run_id, None, error=f"{type(exc).__name__}: {exc}")

        task = asyncio.create_task(job(), name=f"backtest-{run_id}")
        self.backtest_tasks[run_id] = task
        if wait:
            await task
        return run_id


def rejected(exc: IntentRejected) -> dict[str, Any]:
    return {"error": "intent_rejected", "detail": str(exc), "broker_submission": False}
