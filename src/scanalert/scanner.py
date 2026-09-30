"""Event scanner (transition detection) and snapshot scanner (ranked Top List).

Both use the *same* filter/feature code as the backtester, so live and historical results share logic.
Pipelines are separate on purpose (Trade Ideas distinguishes Alert Windows from Top Lists).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .calendar import NyseCalendar
from .features import FEATURES, FeatureContext, SymbolState, compute_all
from .filters import EvalContext, FilterResult, evaluate_all
from .formula import compile_formula
from .models import iso
from .strategy import AlertCondition, StrategySpec


@dataclass
class Signal:
    """A detected trigger, before alert-manager suppression rules are applied."""

    strategy: StrategySpec
    condition: AlertCondition
    symbol: str
    as_of: datetime
    source_ts: datetime
    session: str
    direction: str
    trigger_price: float | None
    bid: float | None
    ask: float | None
    spread_bps: float | None
    feature_snapshot: dict[str, Any]
    filter_snapshot: dict[str, Any]
    data_age_seconds: float | None = None


def _spread_bps(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    mid = (bid + ask) / 2
    return round((ask - bid) / mid * 10_000, 3)


def _snap(results: list[FilterResult]) -> list[dict[str, Any]]:
    return [r.as_dict() for r in results]


class EventScanner:
    """Emits a :class:`Signal` when an alert condition transitions false -> true for a symbol."""

    def __init__(self, calendar: NyseCalendar | None = None):
        self.calendar = calendar or NyseCalendar()
        self._latched: dict[tuple[str, int, str, str], bool] = {}

    def reset(self) -> None:
        self._latched.clear()

    def evaluate(
        self,
        strategy: StrategySpec,
        state: SymbolState,
        as_of: datetime,
        universe: set[str] | None = None,
    ) -> list[Signal]:
        if not strategy.enabled or not strategy.alert_conditions or not state.active:
            return []
        if universe is not None and state.symbol not in universe:
            return []
        session = self.calendar.session_at(as_of)
        if not self.calendar.session_allowed(session):
            return []
        signals: list[Signal] = []
        ectx = EvalContext(state, as_of)
        gate_ok = False if state.halted else evaluate_all(strategy.filters, ectx, short_circuit=True)[0]
        for cond in strategy.alert_conditions:
            key = (strategy.id, strategy.version, cond.id, state.symbol)
            now_true = False
            if gate_ok:
                now_true = evaluate_all(cond.filters, ectx, short_circuit=True)[0]
            was = self._latched.get(key, False)
            self._latched[key] = now_true
            if now_true and not was:
                signals.append(self._build(strategy, cond, state, as_of, session, ectx))
        return signals

    def _build(
        self,
        strategy: StrategySpec,
        cond: AlertCondition,
        state: SymbolState,
        as_of: datetime,
        session: str,
        ectx: EvalContext,
    ) -> Signal:
        basis = "regular" if session == "regular" else "extended"
        _, gate_full = evaluate_all(strategy.filters, ectx)
        _, cond_full = evaluate_all(cond.filters, ectx)
        feats = compute_all(state, as_of, basis)
        last = feats.get("last")
        source_ts = state.last_event_ts or as_of
        return Signal(
            strategy=strategy,
            condition=cond,
            symbol=state.symbol,
            as_of=as_of,
            source_ts=min(source_ts, as_of) if source_ts else as_of,
            session=session,
            direction=strategy.effective_direction(cond),
            trigger_price=float(last)
            if isinstance(last, float | int) and not isinstance(last, bool)
            else None,
            bid=state.bid,
            ask=state.ask,
            spread_bps=_spread_bps(state.bid, state.ask),
            feature_snapshot=feats,
            filter_snapshot={
                "strategy_id": strategy.id,
                "strategy_version": strategy.version,
                "condition_id": cond.id,
                "strategy_filters": _snap(gate_full),
                "condition_filters": _snap(cond_full),
                "config": strategy.config_snapshot(),
            },
            data_age_seconds=(as_of - state.last_event_ts).total_seconds() if state.last_event_ts else None,
        )

    def still_true(self, strategy: StrategySpec, cond_id: str, state: SymbolState, as_of: datetime) -> bool:
        cond = next((c for c in strategy.alert_conditions if c.id == cond_id), None)
        if cond is None or state.halted:
            return False
        ectx = EvalContext(state, as_of)
        return evaluate_all(strategy.filters, ectx, True)[0] and evaluate_all(cond.filters, ectx, True)[0]


# ============================================================================== snapshot scanner
@dataclass
class TopListRow:
    rank: int
    symbol: str
    score: float | None
    last: float | None
    pct_change: float | None
    rvol: float | None
    volume: float | None
    spread_bps: float | None
    features: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "symbol": self.symbol,
            "score": None if self.score is None else round(self.score, 6),
            "last": self.last,
            "pct_change": None if self.pct_change is None else round(self.pct_change, 4),
            "rvol": None if self.rvol is None else round(self.rvol, 3),
            "volume": self.volume,
            "spread_bps": self.spread_bps,
            "features": self.features,
        }


@dataclass
class TopListSnapshot:
    strategy_id: str
    strategy_version: int
    config_hash: str
    as_of: datetime
    generated_at: datetime
    evaluated: int
    qualified: int
    rows: list[TopListRow]
    data_age_seconds: float | None
    stale: bool
    ranking: dict[str, Any]
    display_sort: dict[str, Any] | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "config_hash": self.config_hash,
            "as_of": iso(self.as_of),
            "generated_at": iso(self.generated_at),
            "evaluated": self.evaluated,
            "qualified": self.qualified,
            "returned": len(self.rows),
            "rows": [r.as_dict() for r in self.rows],
            "data_age_seconds": self.data_age_seconds,
            "stale": self.stale,
            "ranking": self.ranking,
            "display_sort": self.display_sort,
            "paper_only": True,
            "label": "HYPOTHETICAL SCAN RESULT - NOT AN ORDER",
        }


def _sort_key_desc(v: float | None, symbol: str, desc: bool) -> tuple[Any, ...]:
    # None always last; ties broken by symbol ascending => deterministic.
    if v is None:
        return (1, 0.0, symbol)
    return (0, -v if desc else v, symbol)


class SnapshotScanner:
    """Periodically ranks every qualifying symbol and returns the top N."""

    def __init__(self, calendar: NyseCalendar | None = None, stale_seconds: float = 15.0):
        self.calendar = calendar or NyseCalendar()
        self.stale_seconds = stale_seconds

    def run(
        self,
        strategy: StrategySpec,
        states: Mapping[str, SymbolState],
        as_of: datetime,
        max_rows: int | None = None,
        generated_at: datetime | None = None,
        universe: set[str] | None = None,
    ) -> TopListSnapshot:
        limit = (
            min(max_rows or strategy.top_list.max_rows, strategy.top_list.max_rows)
            if max_rows
            else strategy.top_list.max_rows
        )
        rank_formula = compile_formula(strategy.ranking.formula) if strategy.ranking.formula else None
        desc = strategy.ranking.order == "desc"
        scored: list[tuple[tuple[Any, ...], str, float | None, SymbolState]] = []
        evaluated = 0
        ages: list[float] = []
        for symbol in sorted(states):
            st = states[symbol]
            if universe is not None and symbol not in universe:
                continue
            if not st.active or st.halted:
                continue
            evaluated += 1
            ectx = EvalContext(st, as_of)
            ok = evaluate_all(strategy.filters, ectx, short_circuit=True)[0]
            if ok and strategy.alert_conditions:
                ok = any(evaluate_all(c.filters, ectx, True)[0] for c in strategy.alert_conditions)
            if not ok:
                continue
            ctx = ectx.for_basis("regular")
            if rank_formula is not None:
                v = rank_formula.evaluate(ctx)
                score = float(v) if isinstance(v, float | int) and not isinstance(v, bool) else None
            else:
                v = ctx.get(strategy.ranking.field)
                score = float(v) if isinstance(v, float | int) and not isinstance(v, bool) else None
            scored.append((_sort_key_desc(score, symbol, desc), symbol, score, st))
            if st.last_event_ts:
                ages.append(max(0.0, (as_of - st.last_event_ts).total_seconds()))
        scored.sort(key=lambda t: t[0])
        top = scored[:limit]
        rows: list[TopListRow] = []
        for i, (_, symbol, score, st) in enumerate(top, start=1):
            ctx = FeatureContext(st, as_of, "regular")
            rows.append(
                TopListRow(
                    rank=i,
                    symbol=symbol,
                    score=score,
                    last=_n(ctx.get("last")),
                    pct_change=_n(ctx.get("pct_change")),
                    rvol=_n(ctx.get("rvol")),
                    volume=_n(ctx.get("volume")),
                    spread_bps=_n(ctx.get("spread_bps")),
                    features={
                        k: _r(ctx.get(k)) for k in ("vwap_dist_pct", "gap_pct", "range_pct", "dollar_volume")
                    },
                )
            )
        ds = strategy.display_sort
        if ds is not None:
            ddesc = ds.order == "desc"
            if ds.field == "symbol":
                rows.sort(key=lambda r: r.symbol, reverse=ddesc)
            else:
                ctxs = {r.symbol: FeatureContext(states[r.symbol], as_of, "regular") for r in rows}
                rows.sort(
                    key=lambda r: (
                        _sort_key_desc(_n(ctxs[r.symbol].get(ds.field)), r.symbol, ddesc)
                        if ds.field in FEATURES and FEATURES[ds.field].type == "number"
                        else (0, r.rank, r.symbol)
                    )
                )
        age = max(ages) if ages else None
        return TopListSnapshot(
            strategy_id=strategy.id,
            strategy_version=strategy.version,
            config_hash=str(strategy.config_snapshot()["config_hash"]),
            as_of=as_of,
            generated_at=generated_at or as_of,
            evaluated=evaluated,
            qualified=len(scored),
            rows=rows,
            data_age_seconds=age,
            stale=age is not None and age > self.stale_seconds,
            ranking=strategy.ranking.model_dump(),
            display_sort=ds.model_dump() if ds else None,
        )


def _n(v: Any) -> float | None:
    return float(v) if isinstance(v, float | int) and not isinstance(v, bool) else None


def _r(v: Any) -> Any:
    return round(v, 4) if isinstance(v, float) else v
