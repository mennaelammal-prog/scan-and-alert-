"""Database access: engine factory, SQL migrations and a repository ("Store").

Uses SQLAlchemy Core with plain, dialect-neutral SQL so the same schema runs on SQLite (development
fallback) and PostgreSQL. ``INSERT .. ON CONFLICT`` upserts are supported by both.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from importlib import resources
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.pool import StaticPool

from .alerts import AlertEvent
from .formula import compile_formula
from .models import Bar, MarketEvent, Quote, Trade, iso, utcnow
from .notify import Delivery, UserPreferences
from .strategy import StrategySpec


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        kwargs: dict[str, Any] = {"connect_args": {"check_same_thread": False}}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool
        else:
            path = url.split("///", 1)[-1]
            if path:
                import pathlib

                pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, **kwargs)

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn: Any, _rec: Any) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

        return engine
    return create_engine(url, pool_pre_ping=True)


def _migration_files() -> list[tuple[str, str]]:
    root = resources.files("scanalert").joinpath("migrations")
    files = sorted((p for p in root.iterdir() if p.name.endswith(".sql")), key=lambda p: p.name)
    return [(p.name, p.read_text(encoding="utf-8")) for p in files]


def _statements(sql: str) -> list[str]:
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def migrate(engine: Engine) -> list[str]:
    """Apply pending migrations in order; returns the names applied. Checksums guard applied files."""
    applied: list[str] = []
    with engine.begin() as cx:
        cx.execute(
            text(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
        )
    for name, sql in _migration_files():
        checksum = hashlib.sha256(sql.encode()).hexdigest()
        with engine.begin() as cx:
            row = cx.execute(
                text("SELECT checksum FROM schema_migrations WHERE version=:v"), {"v": name}
            ).fetchone()
            if row:
                if row[0] != checksum:
                    raise RuntimeError(f"migration {name} was modified after being applied")
                continue
            for stmt in _statements(sql):
                cx.execute(text(stmt))
            cx.execute(
                text("INSERT INTO schema_migrations (version, checksum, applied_at) VALUES (:v,:c,:t)"),
                {"v": name, "c": checksum, "t": iso(utcnow())},
            )
            applied.append(name)
    return applied


def _j(obj: Any) -> str:
    return json.dumps(obj, default=str, sort_keys=True)


def _b(v: Any) -> int:
    return 1 if v else 0


class Store:
    def __init__(self, engine: Engine, provider: str = "", feed: str = ""):
        self.engine, self.provider, self.feed = engine, provider, feed
        # One lock serialises access: the service writes from the event-loop thread and from worker threads,
        # and an in-memory SQLite database shares a single connection that is not thread-safe.
        self._lock = threading.RLock()

    @contextmanager
    def _tx(self) -> Iterator[Any]:
        with self._lock, self.engine.begin() as cx:
            yield cx

    @contextmanager
    def _ro(self) -> Iterator[Any]:
        with self._lock, self.engine.connect() as cx:
            yield cx

    # ------------------------------------------------------------------ helpers
    def _all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        with self._ro() as cx:
            return [dict(r._mapping) for r in cx.execute(text(sql), params or {})]

    def _one(self, sql: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        rows = self._all(sql, params)
        return rows[0] if rows else None

    def _exec(self, sql: str, params: dict[str, Any] | list[dict[str, Any]] | None = None) -> None:
        with self._tx() as cx:
            cx.execute(text(sql), params or {})

    def table_names(self) -> set[str]:
        from sqlalchemy import inspect

        return set(inspect(self.engine).get_table_names())

    # ------------------------------------------------------------ market data
    def upsert_symbols(self, symbols: Iterable[str]) -> None:
        now = iso(utcnow())
        rows = [{"s": s, "t": now} for s in symbols]
        if rows:
            self._exec(
                "INSERT INTO symbols (symbol, active, status, updated_at) VALUES (:s, 1, 'active', :t) "
                "ON CONFLICT (symbol) DO NOTHING",
                rows,
            )

    def set_symbol_active(self, symbol: str, active: bool, status: str | None = None) -> None:
        self._exec(
            "UPDATE symbols SET active=:a, status=:st, updated_at=:t WHERE symbol=:s",
            {
                "a": _b(active),
                "st": status or ("active" if active else "inactive"),
                "t": iso(utcnow()),
                "s": symbol,
            },
        )

    def list_symbols(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM symbols ORDER BY symbol")

    def record_sessions(self, cal: Any, days: Iterable[Any], version: str = "nyse-rules-v1") -> None:
        rows = []
        for d in days:
            trading = cal.is_trading_day(d)
            rows.append(
                {
                    "d": d.isoformat(),
                    "tr": _b(trading),
                    "o": iso(cal.open_dt(d)) if trading else None,
                    "c": iso(cal.close_dt(d)) if trading else None,
                    "e": _b(trading and cal.is_early_close(d)),
                    "h": None if trading else cal.holiday_name(d),
                    "v": version,
                }
            )
        if rows:
            self._exec(
                "INSERT INTO market_sessions (session_date,is_trading_day,open_utc,close_utc,early_close,holiday_name,calendar_version) "
                "VALUES (:d,:tr,:o,:c,:e,:h,:v) ON CONFLICT (session_date) DO UPDATE SET is_trading_day=excluded.is_trading_day, "
                "open_utc=excluded.open_utc, close_utc=excluded.close_utc, early_close=excluded.early_close, "
                "holiday_name=excluded.holiday_name, calendar_version=excluded.calendar_version",
                rows,
            )

    def save_raw_events(self, events: Sequence[MarketEvent]) -> None:
        rows = []
        for ev in events:
            payload = (
                {k: getattr(ev, k) for k in ev.__slots__ if k not in ("ingest_ts",)}
                if hasattr(ev, "__slots__")
                else {}
            )
            rows.append(
                {
                    "k": ev.event_key,
                    "p": self.provider,
                    "f": self.feed,
                    "kind": ev.kind,
                    "s": getattr(ev, "symbol", None),
                    "ts": iso(getattr(ev, "ts", None)),
                    "i": iso(ev.ingest_ts),
                    "pl": _j(payload),
                }
            )
        if rows:
            self._exec(
                "INSERT INTO raw_market_events (event_key,provider,feed,kind,symbol,source_ts,ingest_ts,payload) "
                "VALUES (:k,:p,:f,:kind,:s,:ts,:i,:pl) ON CONFLICT (event_key) DO NOTHING",
                rows,
            )

    def save_bars(self, bars: Sequence[Bar]) -> None:
        rows = [
            {
                "s": b.symbol,
                "tf": b.timeframe,
                "ts": iso(b.ts),
                "r": b.revision,
                "o": b.open,
                "h": b.high,
                "l": b.low,
                "c": b.close,
                "v": b.volume,
                "vw": b.vwap,
                "n": b.trade_count,
                "co": _b(b.corrected),
                "i": iso(b.ingest_ts),
                "p": self.provider,
                "f": self.feed,
            }
            for b in bars
        ]
        if rows:
            self._exec(
                "INSERT INTO bars (symbol,timeframe,bar_ts,revision,open,high,low,close,volume,vwap,trade_count,corrected,ingest_ts,provider,feed) "
                "VALUES (:s,:tf,:ts,:r,:o,:h,:l,:c,:v,:vw,:n,:co,:i,:p,:f) ON CONFLICT (symbol,timeframe,bar_ts,revision) DO UPDATE SET "
                "open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, volume=excluded.volume, "
                "vwap=excluded.vwap, trade_count=excluded.trade_count, corrected=excluded.corrected",
                rows,
            )

    def save_quotes(self, quotes: Sequence[Quote]) -> None:
        rows = [
            {
                "s": q.symbol,
                "ts": iso(q.ts),
                "b": q.bid,
                "a": q.ask,
                "bs": q.bid_size,
                "as_": q.ask_size,
                "i": iso(q.ingest_ts),
                "p": self.provider,
                "f": self.feed,
            }
            for q in quotes
        ]
        if rows:
            self._exec(
                "INSERT INTO quotes (symbol,source_ts,bid,ask,bid_size,ask_size,ingest_ts,provider,feed) "
                "VALUES (:s,:ts,:b,:a,:bs,:as_,:i,:p,:f) ON CONFLICT DO NOTHING",
                rows,
            )

    def save_trades(self, trades: Sequence[Trade]) -> None:
        rows = [
            {
                "s": t.symbol,
                "ts": iso(t.ts),
                "id": t.trade_id,
                "p": t.price,
                "z": t.size,
                "c": ",".join(t.conditions),
                "i": iso(t.ingest_ts),
                "pv": self.provider,
                "f": self.feed,
            }
            for t in trades
        ]
        if rows:
            self._exec(
                "INSERT INTO trades (symbol,source_ts,trade_id,price,size,conditions,ingest_ts,provider,feed) "
                "VALUES (:s,:ts,:id,:p,:z,:c,:i,:pv,:f) ON CONFLICT DO NOTHING",
                rows,
            )

    def load_bars(
        self,
        symbol: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 100_000,
    ) -> list[dict[str, Any]]:
        # Latest revision per (symbol, ts) wins.
        sql = (
            "SELECT b.* FROM bars b JOIN (SELECT symbol, timeframe, bar_ts, MAX(revision) AS r FROM bars GROUP BY symbol, timeframe, bar_ts) m "
            "ON b.symbol=m.symbol AND b.timeframe=m.timeframe AND b.bar_ts=m.bar_ts AND b.revision=m.r WHERE 1=1"
        )
        params: dict[str, Any] = {"lim": limit}
        if symbol:
            sql += " AND b.symbol=:s"
            params["s"] = symbol
        if start:
            sql += " AND b.bar_ts>=:a"
            params["a"] = start
        if end:
            sql += " AND b.bar_ts<:z"
            params["z"] = end
        return self._all(sql + " ORDER BY b.bar_ts, b.symbol LIMIT :lim", params)

    def count(self, table: str) -> int:
        assert table.isidentifier()
        row = self._one(f"SELECT COUNT(*) AS n FROM {table}")  # noqa: S608 - identifier validated above
        return int(row["n"]) if row else 0

    # -------------------------------------------------------------- strategies
    def _write_version(self, cx: Any, spec: StrategySpec, now: str) -> None:
        snap = spec.config_snapshot()
        cx.execute(
            text(
                "INSERT INTO strategy_versions (strategy_id,version,spec_json,config_hash,created_at) VALUES (:i,:v,:j,:h,:t)"
            ),
            {
                "i": spec.id,
                "v": spec.version,
                "j": _j(spec.model_dump(mode="json")),
                "h": snap["config_hash"],
                "t": now,
            },
        )
        scoped = [("gate", f) for f in spec.filters] + [
            (f"condition:{c.id}", f) for c in spec.alert_conditions for f in c.filters
        ]
        for scope, f in scoped:
            cx.execute(
                text(
                    "INSERT INTO filters (strategy_id,strategy_version,scope,filter_id,filter_version,name,field,operator,value_json,unit,"
                    "session_basis,lookback,null_policy,enabled,description) VALUES (:i,:v,:sc,:fid,:fv,:n,:fld,:op,:val,:u,:sb,:lb,:np,:en,:d)"
                ),
                {
                    "i": spec.id,
                    "v": spec.version,
                    "sc": scope,
                    "fid": f.id,
                    "fv": f.version,
                    "n": f.name,
                    "fld": f.field,
                    "op": f.operator,
                    "val": _j(f.value),
                    "u": f.resolved_unit,
                    "sb": f.session_basis,
                    "lb": f.lookback,
                    "np": f.null_policy,
                    "en": _b(f.enabled),
                    "d": f.description,
                },
            )
            if f.formula:
                cf = compile_formula(f.formula, expect="bool")
                cx.execute(
                    text(
                        "INSERT INTO formula_versions (strategy_id,strategy_version,formula_id,expression,canonical,digest) VALUES (:i,:v,:f,:e,:c,:d)"
                    ),
                    {
                        "i": spec.id,
                        "v": spec.version,
                        "f": f"{scope}:{f.id}",
                        "e": f.formula,
                        "c": cf.canonical,
                        "d": cf.digest,
                    },
                )
        if spec.ranking.formula:
            cf = compile_formula(spec.ranking.formula)
            cx.execute(
                text(
                    "INSERT INTO formula_versions (strategy_id,strategy_version,formula_id,expression,canonical,digest) VALUES (:i,:v,'ranking',:e,:c,:d)"
                ),
                {
                    "i": spec.id,
                    "v": spec.version,
                    "e": spec.ranking.formula,
                    "c": cf.canonical,
                    "d": cf.digest,
                },
            )

    def create_strategy(self, spec: StrategySpec) -> StrategySpec:
        if self._one("SELECT 1 AS x FROM strategies WHERE strategy_id=:i", {"i": spec.id}):
            raise ValueError(f"strategy {spec.id!r} already exists; use update to create a new version")
        spec = spec.model_copy(update={"version": 1})
        now = iso(utcnow()) or ""
        with self._tx() as cx:
            cx.execute(
                text(
                    "INSERT INTO strategies (strategy_id,name,current_version,enabled,created_at,updated_at) VALUES (:i,:n,1,:e,:t,:t)"
                ),
                {"i": spec.id, "n": spec.name, "e": _b(spec.enabled), "t": now},
            )
            self._write_version(cx, spec, now)
        self.audit(
            "api", "strategy.create", "strategy", spec.id, f"v1 hash={spec.config_snapshot()['config_hash']}"
        )
        return spec

    def update_strategy(self, strategy_id: str, spec: StrategySpec) -> StrategySpec:
        cur = self._one("SELECT current_version FROM strategies WHERE strategy_id=:i", {"i": strategy_id})
        if not cur:
            raise KeyError(strategy_id)
        spec = spec.model_copy(update={"id": strategy_id, "version": int(cur["current_version"]) + 1})
        now = iso(utcnow()) or ""
        with self._tx() as cx:
            self._write_version(cx, spec, now)
            cx.execute(
                text(
                    "UPDATE strategies SET name=:n, current_version=:v, enabled=:e, updated_at=:t WHERE strategy_id=:i"
                ),
                {"n": spec.name, "v": spec.version, "e": _b(spec.enabled), "t": now, "i": strategy_id},
            )
        self.audit(
            "api",
            "strategy.update",
            "strategy",
            strategy_id,
            f"v{spec.version} hash={spec.config_snapshot()['config_hash']}",
        )
        return spec

    def get_strategy(self, strategy_id: str, version: int | None = None) -> StrategySpec | None:
        if version is None:
            row = self._one(
                "SELECT v.spec_json FROM strategies s JOIN strategy_versions v ON v.strategy_id=s.strategy_id AND v.version=s.current_version "
                "WHERE s.strategy_id=:i",
                {"i": strategy_id},
            )
        else:
            row = self._one(
                "SELECT spec_json FROM strategy_versions WHERE strategy_id=:i AND version=:v",
                {"i": strategy_id, "v": version},
            )
        return StrategySpec.model_validate_json(row["spec_json"]) if row else None

    def list_strategies(self) -> list[StrategySpec]:
        rows = self._all(
            "SELECT v.spec_json FROM strategies s JOIN strategy_versions v ON v.strategy_id=s.strategy_id AND v.version=s.current_version ORDER BY s.strategy_id"
        )
        return [StrategySpec.model_validate_json(r["spec_json"]) for r in rows]

    def strategy_versions(self, strategy_id: str) -> list[dict[str, Any]]:
        return self._all(
            "SELECT version, config_hash, created_at FROM strategy_versions WHERE strategy_id=:i ORDER BY version",
            {"i": strategy_id},
        )

    # ------------------------------------------------------------------ alerts
    def save_alert(self, a: AlertEvent) -> None:
        self._exec(
            "INSERT INTO alert_events (event_id,symbol,strategy_id,strategy_version,condition_id,direction,event_type,source_ts,detected_ts,session,"
            "trigger_price,bid,ask,spread_bps,status,status_reason,priority,dedupe_key,expires_at,confirm_at,acknowledged_at,acknowledged_by,delivered_ts,"
            "delivery_delay_ms,stale_data,delayed_delivery,data_provider,data_feed,strategy_config_hash,feature_snapshot,filter_snapshot,paper_only) "
            "VALUES (:event_id,:symbol,:strategy_id,:strategy_version,:condition_id,:direction,:event_type,:source_ts,:detected_ts,:session,"
            ":trigger_price,:bid,:ask,:spread_bps,:status,:status_reason,:priority,:dedupe_key,:expires_at,:confirm_at,:acknowledged_at,:acknowledged_by,"
            ":delivered_ts,:delivery_delay_ms,:stale_data,:delayed_delivery,:data_provider,:data_feed,:strategy_config_hash,:feature_snapshot,:filter_snapshot,1) "
            "ON CONFLICT (event_id) DO UPDATE SET status=excluded.status, status_reason=excluded.status_reason, acknowledged_at=excluded.acknowledged_at, "
            "acknowledged_by=excluded.acknowledged_by, delivered_ts=excluded.delivered_ts, delivery_delay_ms=excluded.delivery_delay_ms, "
            "delayed_delivery=excluded.delayed_delivery, stale_data=excluded.stale_data",
            {
                "event_id": a.event_id,
                "symbol": a.symbol,
                "strategy_id": a.strategy_id,
                "strategy_version": a.strategy_version,
                "condition_id": a.condition_id,
                "direction": a.direction,
                "event_type": a.event_type,
                "source_ts": a.source_timestamp,
                "detected_ts": a.detected_timestamp,
                "session": a.session,
                "trigger_price": a.trigger_price,
                "bid": a.bid,
                "ask": a.ask,
                "spread_bps": a.spread_bps,
                "status": a.status,
                "status_reason": a.status_reason,
                "priority": a.priority,
                "dedupe_key": a.dedupe_key,
                "expires_at": a.expires_at,
                "confirm_at": a.confirm_at,
                "acknowledged_at": a.acknowledged_at,
                "acknowledged_by": a.acknowledged_by,
                "delivered_ts": a.delivered_timestamp,
                "delivery_delay_ms": a.delivery_delay_ms,
                "stale_data": _b(a.stale_data),
                "delayed_delivery": _b(a.delayed_delivery),
                "data_provider": a.data_provider,
                "data_feed": a.data_feed,
                "strategy_config_hash": a.strategy_config_hash,
                "feature_snapshot": _j(a.feature_snapshot),
                "filter_snapshot": _j(a.filter_snapshot),
            },
        )

    @staticmethod
    def _alert_from_row(r: dict[str, Any]) -> AlertEvent:
        return AlertEvent(
            event_id=r["event_id"],
            symbol=r["symbol"],
            strategy_id=r["strategy_id"],
            strategy_version=r["strategy_version"],
            condition_id=r["condition_id"],
            direction=r["direction"],
            event_type=r["event_type"],
            source_timestamp=r["source_ts"],
            detected_timestamp=r["detected_ts"],
            session=r["session"],
            trigger_price=r["trigger_price"],
            bid=r["bid"],
            ask=r["ask"],
            spread_bps=r["spread_bps"],
            feature_snapshot=json.loads(r["feature_snapshot"]),
            filter_snapshot=json.loads(r["filter_snapshot"]),
            status=r["status"],
            status_reason=r["status_reason"] or "",
            priority=r["priority"],
            dedupe_key=r["dedupe_key"] or "",
            expires_at=r["expires_at"],
            confirm_at=r["confirm_at"],
            acknowledged_at=r["acknowledged_at"],
            acknowledged_by=r["acknowledged_by"],
            delivered_timestamp=r["delivered_ts"],
            delivery_delay_ms=r["delivery_delay_ms"],
            stale_data=bool(r["stale_data"]),
            delayed_delivery=bool(r["delayed_delivery"]),
            data_provider=r["data_provider"] or "",
            data_feed=r["data_feed"] or "",
            strategy_config_hash=r["strategy_config_hash"] or "",
        )

    def get_alert(self, event_id: str) -> AlertEvent | None:
        r = self._one("SELECT * FROM alert_events WHERE event_id=:i", {"i": event_id})
        return self._alert_from_row(r) if r else None

    def list_alerts(
        self,
        status: str | None = None,
        symbol: str | None = None,
        strategy_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AlertEvent]:
        sql = "SELECT * FROM alert_events WHERE 1=1"
        params: dict[str, Any] = {"lim": limit, "off": offset}
        for col, val in (("status", status), ("symbol", symbol), ("strategy_id", strategy_id)):
            if val:
                sql += f" AND {col}=:{col}"
                params[col] = val
        rows = self._all(sql + " ORDER BY source_ts DESC, event_id LIMIT :lim OFFSET :off", params)
        return [self._alert_from_row(r) for r in rows]

    def save_delivery(self, d: Delivery) -> None:
        self._exec(
            "INSERT INTO alert_deliveries (delivery_id,event_id,channel,status,attempts,reason,error,source_ts,detected_ts,delivered_ts,latency_ms,kind,created_at) "
            "VALUES (:delivery_id,:event_id,:channel,:status,:attempts,:reason,:error,:source_ts,:detected_ts,:delivered_ts,:latency_ms,:kind,:created_at) "
            "ON CONFLICT (delivery_id) DO NOTHING",
            {**d.as_dict(), "created_at": iso(utcnow())},
        )

    def list_deliveries(self, event_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if event_id:
            return self._all(
                "SELECT * FROM alert_deliveries WHERE event_id=:i ORDER BY created_at LIMIT :l",
                {"i": event_id, "l": limit},
            )
        return self._all("SELECT * FROM alert_deliveries ORDER BY created_at DESC LIMIT :l", {"l": limit})

    # --------------------------------------------------------------- backtests
    def create_backtest_run(
        self, run_id: str, spec: StrategySpec, params: dict[str, Any], provider: str, feed: str
    ) -> None:
        snap = spec.config_snapshot()
        self._exec(
            "INSERT INTO backtest_runs (run_id,strategy_id,strategy_version,config_hash,status,created_at,date_start,date_end,data_provider,data_feed,params_json,strategy_snapshot) "
            "VALUES (:r,:s,:v,:h,'running',:t,:ds,:de,:p,:f,:pj,:ss)",
            {
                "r": run_id,
                "s": spec.id,
                "v": spec.version,
                "h": snap["config_hash"],
                "t": iso(utcnow()),
                "ds": params.get("start_date"),
                "de": params.get("end_date"),
                "p": provider,
                "f": feed,
                "pj": _j(params),
                "ss": _j(snap),
            },
        )

    def finish_backtest_run(
        self, run_id: str, report: dict[str, Any] | None, error: str | None = None
    ) -> None:
        self._exec(
            "UPDATE backtest_runs SET status=:st, finished_at=:t, report_json=:r, error=:e WHERE run_id=:i",
            {
                "st": "failed" if error else "completed",
                "t": iso(utcnow()),
                "r": _j(report) if report is not None else None,
                "e": error,
                "i": run_id,
            },
        )

    def get_backtest_run(self, run_id: str) -> dict[str, Any] | None:
        r = self._one("SELECT * FROM backtest_runs WHERE run_id=:i", {"i": run_id})
        if not r:
            return None
        r["params"] = json.loads(r.pop("params_json"))
        r["strategy_snapshot"] = json.loads(r["strategy_snapshot"])
        r["report"] = json.loads(r.pop("report_json")) if r.get("report_json") else None
        return r

    def list_backtest_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._all(
            "SELECT run_id,strategy_id,strategy_version,config_hash,status,created_at,finished_at,date_start,date_end,label FROM backtest_runs ORDER BY created_at DESC LIMIT :l",
            {"l": limit},
        )

    def save_backtest_trades(self, run_id: str, trades: Sequence[dict[str, Any]]) -> None:
        rows = [
            {
                "r": run_id,
                "n": i,
                "s": t["symbol"],
                "d": t["direction"],
                "et": t["entry_ts"],
                "ep": t["entry_price"],
                "xt": t["exit_ts"],
                "xp": t["exit_price"],
                "q": t["quantity"],
                "g": t["gross_pnl"],
                "c": t["costs"],
                "np": t["net_pnl"],
                "er": t["exit_reason"],
                "ev": t.get("event_id"),
                "dj": _j(t),
            }
            for i, t in enumerate(trades, start=1)
        ]
        if rows:
            self._exec(
                "INSERT INTO backtest_trades (run_id,trade_no,symbol,direction,entry_ts,entry_price,exit_ts,exit_price,quantity,gross_pnl,costs,net_pnl,exit_reason,event_id,detail_json) "
                "VALUES (:r,:n,:s,:d,:et,:ep,:xt,:xp,:q,:g,:c,:np,:er,:ev,:dj)",
                rows,
            )

    def list_backtest_trades(self, run_id: str, limit: int = 1000, offset: int = 0) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT detail_json FROM backtest_trades WHERE run_id=:r ORDER BY trade_no LIMIT :l OFFSET :o",
            {"r": run_id, "l": limit, "o": offset},
        )
        return [json.loads(r["detail_json"]) for r in rows]

    # ------------------------------------------------------------ paper trading
    def save_paper_intent(self, d: dict[str, Any]) -> None:
        self._exec(
            "INSERT INTO paper_intents (intent_id,event_id,symbol,direction,quantity,entry_model,stop_model,target_model,time_exit_minutes,est_spread_bps,"
            "est_slippage_bps,max_modeled_loss,strategy_id,strategy_version,status,created_at,expires_at,detail_json,simulated,submitted_to_broker) "
            "VALUES (:intent_id,:event_id,:symbol,:direction,:quantity,:entry_model,:stop_model,:target_model,:time_exit_minutes,:est_spread_bps,"
            ":est_slippage_bps,:max_modeled_loss,:strategy_id,:strategy_version,:status,:created_at,:expires_at,:detail_json,1,0) "
            "ON CONFLICT (intent_id) DO UPDATE SET status=excluded.status, detail_json=excluded.detail_json",
            {
                **d,
                "stop_model": _j(d["stop_model"]),
                "target_model": _j(d["target_model"]),
                "detail_json": _j(d.get("detail", {})),
            },
        )

    def get_paper_intent(self, intent_id: str) -> dict[str, Any] | None:
        r = self._one("SELECT * FROM paper_intents WHERE intent_id=:i", {"i": intent_id})
        return self._intent_row(r) if r else None

    @staticmethod
    def _intent_row(r: dict[str, Any]) -> dict[str, Any]:
        r = dict(r)
        r["stop_model"], r["target_model"] = json.loads(r["stop_model"]), json.loads(r["target_model"])
        r["detail"] = json.loads(r.pop("detail_json"))
        r["simulated"], r["submitted_to_broker"] = bool(r["simulated"]), bool(r["submitted_to_broker"])
        return r

    def list_paper_intents(self, limit: int = 100) -> list[dict[str, Any]]:
        return [
            self._intent_row(r)
            for r in self._all("SELECT * FROM paper_intents ORDER BY created_at DESC LIMIT :l", {"l": limit})
        ]

    def save_paper_fill(self, f: dict[str, Any]) -> None:
        self._exec(
            "INSERT INTO paper_fills (fill_id,intent_id,symbol,side,quantity,price,fill_ts,model,partial,status,reject_reason,detail_json,simulated) "
            "VALUES (:fill_id,:intent_id,:symbol,:side,:quantity,:price,:fill_ts,:model,:partial,:status,:reject_reason,:detail_json,1)",
            {**f, "partial": _b(f.get("partial")), "detail_json": _j(f.get("detail", {}))},
        )

    def list_paper_fills(self, intent_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        if intent_id:
            rows = self._all(
                "SELECT * FROM paper_fills WHERE intent_id=:i ORDER BY fill_ts", {"i": intent_id}
            )
        else:
            rows = self._all("SELECT * FROM paper_fills ORDER BY fill_ts DESC LIMIT :l", {"l": limit})
        for r in rows:
            r["detail"] = json.loads(r.pop("detail_json"))
            r["simulated"], r["partial"] = bool(r["simulated"]), bool(r["partial"])
        return rows

    # -------------------------------------------------------- audit & preferences
    def audit(
        self,
        actor: str,
        action: str,
        entity_type: str | None = None,
        entity_id: str | None = None,
        detail: str = "",
    ) -> None:
        self._exec(
            "INSERT INTO audit_log (id,ts,actor,action,entity_type,entity_id,detail) VALUES (:id,:t,:a,:ac,:et,:ei,:d)",
            {
                "id": uuid.uuid4().hex,
                "t": iso(utcnow()),
                "a": actor,
                "ac": action,
                "et": entity_type,
                "ei": entity_id,
                "d": detail[:2000],
            },
        )

    def list_audit(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM audit_log ORDER BY ts DESC LIMIT :l", {"l": limit})

    def save_preferences(self, p: UserPreferences) -> None:
        self._exec(
            "INSERT INTO user_preferences (user_id,prefs_json,updated_at) VALUES (:u,:j,:t) "
            "ON CONFLICT (user_id) DO UPDATE SET prefs_json=excluded.prefs_json, updated_at=excluded.updated_at",
            {"u": p.user_id, "j": p.model_dump_json(), "t": iso(utcnow())},
        )

    def load_preferences(self, user_id: str = "default") -> UserPreferences:
        r = self._one("SELECT prefs_json FROM user_preferences WHERE user_id=:u", {"u": user_id})
        return UserPreferences.model_validate_json(r["prefs_json"]) if r else UserPreferences(user_id=user_id)
