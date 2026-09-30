"""Versioned strategy definitions: gate filters (AND), alert conditions (OR), ranking, Top List config."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .features import FEATURES
from .filters import FilterSpec
from .formula import FormulaError, compile_formula

Direction = Literal["long", "short"]
Priority = Literal["low", "normal", "high", "critical"]
PRIORITY_RANK = {"low": 0, "normal": 1, "high": 2, "critical": 3}


class AlertCondition(BaseModel):
    """An event trigger. Fires when its AND-combined filters transition false -> true."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    name: str
    event_type: str = Field("signal", max_length=40)
    direction: Direction | None = None  # None => inherit strategy direction
    priority: Priority = "normal"
    filters: list[FilterSpec] = Field(min_length=1)
    cooldown_seconds: int = Field(900, ge=0)
    dedupe_bucket_seconds: int = Field(300, ge=1)
    confirm_bars: int = Field(0, ge=0, le=30)
    expires_after_seconds: int = Field(3600, ge=1)
    max_alerts_per_symbol_per_day: int = Field(3, ge=1)


class RankingSpec(BaseModel):
    """Server-side ranking. ``field`` is a numeric feature or, if ``formula`` is set, an expression."""

    model_config = ConfigDict(extra="forbid")

    field: str = "rvol"
    formula: str | None = None
    order: Literal["asc", "desc"] = "desc"

    @model_validator(mode="after")
    def _v(self) -> RankingSpec:
        if self.formula:
            try:
                c = compile_formula(self.formula)
            except FormulaError as exc:
                raise ValueError(f"ranking formula error: {exc}") from exc
            if c.type != "number":
                raise ValueError("ranking formula must be numeric")
        else:
            fd = FEATURES.get(self.field)
            if fd is None or fd.type != "number":
                raise ValueError(f"ranking field {self.field!r} must be a numeric feature")
        return self


class DisplaySort(BaseModel):
    """Secondary sort applied to the already-selected top-N rows (like Trade Ideas' display sort)."""

    model_config = ConfigDict(extra="forbid")

    field: str = "pct_change"
    order: Literal["asc", "desc"] = "desc"

    @model_validator(mode="after")
    def _v(self) -> DisplaySort:
        if self.field != "symbol" and self.field not in FEATURES:
            raise ValueError(f"unknown display sort field {self.field!r}")
        return self


class TopListSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_seconds: float = Field(30.0, ge=1)
    max_rows: int = Field(100, ge=1, le=1000)


class StrategySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    name: str = Field(min_length=1, max_length=120)
    version: int = Field(1, ge=1)
    description: str = ""
    enabled: bool = True
    direction: Direction = "long"
    universe: list[str] | None = None  # None => default universe
    filters: list[FilterSpec] = Field(
        default_factory=list
    )  # AND gate applied to every condition and the Top List
    alert_conditions: list[AlertCondition] = Field(default_factory=list)  # OR
    ranking: RankingSpec = Field(default_factory=RankingSpec)
    display_sort: DisplaySort | None = None
    top_list: TopListSpec = Field(default_factory=TopListSpec)

    @model_validator(mode="after")
    def _v(self) -> StrategySpec:
        if not self.filters and not self.alert_conditions:
            raise ValueError("a strategy needs at least one filter or alert condition")
        ids = [f.id for f in self.filters]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate filter ids in strategy filters")
        cids = [c.id for c in self.alert_conditions]
        if len(cids) != len(set(cids)):
            raise ValueError("duplicate alert condition ids")
        if self.universe is not None:
            self.universe = sorted({s.strip().upper() for s in self.universe if s.strip()})
        return self

    def config_snapshot(self) -> dict[str, Any]:
        """Exact configuration preserved with every alert/backtest, with formula digests."""
        snap = self.model_dump(mode="json")
        digests: dict[str, str] = {}
        for f in [*self.filters, *(f for c in self.alert_conditions for f in c.filters)]:
            if f.formula:
                digests[f.id] = compile_formula(f.formula, expect="bool").digest
        if self.ranking.formula:
            digests["ranking"] = compile_formula(self.ranking.formula).digest
        snap["formula_digests"] = digests
        snap["config_hash"] = config_hash(snap)
        return snap

    def effective_direction(self, cond: AlertCondition | None) -> Direction:
        return cond.direction if cond and cond.direction else self.direction


def config_hash(obj: Any) -> str:
    data = {k: v for k, v in obj.items() if k != "config_hash"} if isinstance(obj, dict) else obj
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    return [
        {"path": ".".join(str(p) for p in e["loc"]), "message": e["msg"].removeprefix("Value error, ")}
        for e in exc.errors()
    ]


def validate_strategy(payload: dict[str, Any]) -> dict[str, Any]:
    """Return ``{valid, errors, warnings, spec?}`` without raising."""
    try:
        spec = StrategySpec.model_validate(payload)
    except ValidationError as exc:
        return {"valid": False, "errors": validation_errors(exc), "warnings": []}
    warnings: list[str] = []
    if not spec.alert_conditions:
        warnings.append("no alert conditions: strategy only feeds the Top List, it never emits event alerts")
    for c in spec.alert_conditions:
        if all(not f.enabled for f in c.filters):
            warnings.append(f"alert condition {c.id!r} has all filters disabled and will never trigger")
    if any(f.session_basis != "regular" for f in spec.filters):
        warnings.append(
            "non-regular session basis used; OddsMaker-style backtests use the regular session only"
        )
    return {"valid": True, "errors": [], "warnings": warnings, "spec": spec.model_dump(mode="json")}


# --------------------------------------------------------------------------------------- templates
def _f(id_: str, field: str, op: str, value: Any, **kw: Any) -> FilterSpec:
    return FilterSpec(id=id_, name=kw.pop("name", id_), field=field, operator=op, value=value, **kw)


def opening_range_breakout() -> StrategySpec:
    return StrategySpec(
        id="opening-range-breakout",
        name="Opening Range Breakout (15m)",
        description="Long when price breaks above the 15-minute opening range on elevated relative volume, "
        "above VWAP, with a liquid, tight-spread name. Hypothetical scan only.",
        direction="long",
        filters=[
            _f("min-price", "last", "gte", 2.0, name="Price >= $2"),
            _f("min-dollar-volume", "dollar_volume", "gte", 250_000, name="Session dollar volume >= $250k"),
            _f("max-spread", "spread_bps", "lte", 60, name="Spread <= 60 bps", null_policy="pass"),
        ],
        alert_conditions=[
            AlertCondition(
                id="orb-up",
                name="Breaks above opening range",
                event_type="breakout",
                priority="high",
                filters=[
                    _f("orb-up", "orb_breakout_up", "is_true", None, lookback=15),
                    _f("rvol-1-5", "rvol", "gte", 1.5, null_policy="fail"),
                    _f("above-vwap", "vwap_dist_pct", "gt", 0),
                ],
                cooldown_seconds=1800,
                dedupe_bucket_seconds=900,
            )
        ],
        ranking=RankingSpec(field="rvol", order="desc"),
        display_sort=DisplaySort(field="pct_change", order="desc"),
    )


def momentum_gap() -> StrategySpec:
    return StrategySpec(
        id="momentum-gap",
        name="Gap and Go",
        description="Gapping names with heavy relative volume holding above VWAP (Top List focus).",
        direction="long",
        filters=[
            _f("min-price", "last", "gte", 2.0),
            _f("gap", "gap_pct", "gte", 3.0),
            _f("rvol", "rvol", "gte", 1.5),
            _f(
                "hold-vwap",
                "formula",
                "is_true",
                None,
                formula="vwap_dist_pct > 0 and pct_change > gap_pct * 0.5",
                name="Holds above VWAP and keeps >50% of gap",
            ),
        ],
        alert_conditions=[
            AlertCondition(
                id="new-high",
                name="New 30-bar high",
                event_type="new_high",
                filters=[_f("new-high-30", "new_high", "is_true", None, lookback=30)],
                cooldown_seconds=600,
            )
        ],
        ranking=RankingSpec(formula="rvol * pct_change"),
        display_sort=DisplaySort(field="pct_change", order="desc"),
    )


TEMPLATES = {"opening-range-breakout": opening_range_breakout, "momentum-gap": momentum_gap}
