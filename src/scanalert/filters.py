"""Typed, versioned filters evaluated deterministically against a feature context."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime
from functools import lru_cache
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .features import BASES, FEATURES, FeatureContext, SymbolState
from .formula import CompiledFormula, FormulaError, compile_formula

Operator = Literal["gt", "gte", "lt", "lte", "eq", "neq", "between", "outside", "is_true", "is_false"]
NullPolicy = Literal["fail", "pass", "skip"]
SessionBasis = Literal["regular", "extended", "premarket", "postmarket"]

NUMERIC_OPS = {"gt", "gte", "lt", "lte", "eq", "neq", "between", "outside"}
BOOL_OPS = {"is_true", "is_false", "eq", "neq"}


class FilterSpec(BaseModel):
    """One typed predicate. ``field`` is a feature name, or ``"formula"`` with ``formula`` set."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    name: str = Field(min_length=1, max_length=120)
    version: int = Field(1, ge=1)
    field: str
    operator: Operator
    value: Any = None  # number, bool, [low, high] or null; validated below for clear messages
    unit: str | None = None
    session_basis: SessionBasis = "regular"
    lookback: int | None = None
    null_policy: NullPolicy = "fail"
    enabled: bool = True
    description: str = ""
    formula: str | None = Field(None, max_length=500)

    @model_validator(mode="after")
    def _validate(self) -> FilterSpec:
        if self.field == "formula":
            if not self.formula:
                raise ValueError("field 'formula' requires the 'formula' expression")
            if self.operator not in ("is_true", "is_false"):
                raise ValueError("formula filters must use operator is_true or is_false")
            try:
                compile_formula(self.formula, expect="bool")
            except FormulaError as exc:
                raise ValueError(f"formula error: {exc}") from exc
            if self.lookback is not None:
                raise ValueError("formula filters take lookbacks inside the expression, e.g. sma(20)")
            return self
        if self.formula:
            raise ValueError("'formula' is only allowed when field == 'formula'")
        fd = FEATURES.get(self.field)
        if fd is None:
            raise ValueError(f"unknown field {self.field!r}; known: {', '.join(sorted(FEATURES))}")
        if self.unit is not None and self.unit != fd.unit:
            raise ValueError(
                f"unit mismatch: field {self.field!r} is in {fd.unit!r}, filter declares {self.unit!r}"
            )
        if self.lookback is not None:
            if fd.default_lookback is None:
                raise ValueError(f"field {self.field!r} does not take a lookback")
            if not 1 <= self.lookback <= fd.max_lookback:
                raise ValueError(f"lookback for {self.field!r} must be 1..{fd.max_lookback}")
        if fd.type == "bool":
            if self.operator not in BOOL_OPS:
                raise ValueError(f"boolean field {self.field!r} supports {sorted(BOOL_OPS)}")
            if self.operator in ("eq", "neq") and not isinstance(self.value, bool):
                raise ValueError("eq/neq on a boolean field requires a boolean value")
        else:
            if self.operator not in NUMERIC_OPS:
                raise ValueError(f"numeric field {self.field!r} supports {sorted(NUMERIC_OPS)}")
            if self.operator in ("between", "outside"):
                v = self.value
                if not (
                    isinstance(v, list)
                    and len(v) == 2
                    and all(isinstance(x, int | float) and not isinstance(x, bool) for x in v)
                    and v[0] <= v[1]
                ):
                    raise ValueError("between/outside require value=[low, high] (numbers) with low <= high")
            elif isinstance(self.value, bool) or not isinstance(self.value, int | float):
                raise ValueError(f"operator {self.operator!r} requires a numeric value")
        return self

    @property
    def resolved_unit(self) -> str:
        if self.field == "formula":
            return "bool"
        return FEATURES[self.field].unit


@lru_cache(maxsize=512)
def _compiled(src: str) -> CompiledFormula:
    return compile_formula(src, expect="bool")


@dataclass
class FilterResult:
    filter_id: str
    filter_version: int
    field: str
    operator: str
    threshold: Any
    observed: Any
    passed: bool
    is_null: bool = False
    skipped: bool = False
    notes: list[str] = dc_field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "filter_id": self.filter_id,
            "filter_version": self.filter_version,
            "field": self.field,
            "operator": self.operator,
            "threshold": self.threshold,
            "observed": self.observed,
            "passed": self.passed,
            "null": self.is_null,
            "skipped": self.skipped,
            "notes": self.notes,
        }


class EvalContext:
    """Feature contexts per session basis for one (symbol, as_of)."""

    def __init__(self, state: SymbolState, as_of: datetime):
        self.state, self.as_of = state, as_of
        self._by_basis: dict[str, FeatureContext] = {}

    def for_basis(self, basis: str) -> FeatureContext:
        if basis not in BASES:
            raise ValueError(basis)
        if basis not in self._by_basis:
            self._by_basis[basis] = FeatureContext(self.state, self.as_of, basis)
        return self._by_basis[basis]


def _apply(op: str, x: float | bool, v: Any) -> bool:
    if op == "gt":
        return x > v
    if op == "gte":
        return x >= v
    if op == "lt":
        return x < v
    if op == "lte":
        return x <= v
    if op == "eq":
        return x == v
    if op == "neq":
        return x != v
    if op == "between":
        return v[0] <= x <= v[1]
    if op == "outside":
        return x < v[0] or x > v[1]
    if op == "is_true":
        return bool(x)
    if op == "is_false":
        return not bool(x)
    raise ValueError(op)


def evaluate_filter(f: FilterSpec, ectx: EvalContext) -> FilterResult:
    ctx = ectx.for_basis(f.session_basis)
    notes_before = len(ctx.notes)
    if f.field == "formula":
        assert f.formula
        observed = _compiled(f.formula).evaluate(ctx)
    else:
        observed = ctx.get(f.field, f.lookback)
    notes = ctx.notes[notes_before:]
    res = FilterResult(
        f.id,
        f.version,
        f.field,
        f.operator,
        f.value,
        observed if not isinstance(observed, float) else round(observed, 6),
        False,
        notes=list(notes),
    )
    if observed is None:
        res.is_null = True
        if f.null_policy == "pass":
            res.passed = True
        elif f.null_policy == "skip":
            res.passed, res.skipped = True, True
        else:
            res.passed = False
        return res
    res.passed = _apply(f.operator, observed, f.value)
    return res


def evaluate_all(
    filters: list[FilterSpec], ectx: EvalContext, short_circuit: bool = False
) -> tuple[bool, list[FilterResult]]:
    """AND-combine enabled filters. With ``short_circuit`` stops at the first failure."""
    results: list[FilterResult] = []
    ok = True
    for f in filters:
        if not f.enabled:
            continue
        r = evaluate_filter(f, ectx)
        results.append(r)
        if not r.passed:
            ok = False
            if short_circuit:
                break
    return ok, results
