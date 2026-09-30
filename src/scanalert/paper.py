"""Paper-only order intents and simulated fills.

Nothing in this module can reach a broker: there is no network client, no broker SDK import and no order
endpoint. An "intent" is an *unsubmitted* hypothetical entry; a "fill" is the output of a deterministic
simulator. Every record carries ``simulated=True`` / ``submitted_to_broker=False`` and the database
rejects rows where that is not the case.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .alerts import AlertEvent
from .models import Bar, iso, utcnow

EntryModelName = Literal[
    "next_trade", "next_bar_open", "bid_ask_cross", "fixed_slippage", "spread_plus_slippage"
]
IntrabarPolicy = Literal["stop_first", "target_first", "reject_ambiguous"]
DEFAULT_ASSUMED_SPREAD_BPS = 10.0
PAPER_LABEL = "SIMULATED PAPER INTENT - NOT SUBMITTED TO ANY BROKER"


class StopModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["percent", "dollars", "atr", "none"] = "percent"
    value: float = Field(1.0, gt=0)


class TargetModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["percent", "dollars", "r_multiple", "none"] = "r_multiple"
    value: float = Field(2.0, gt=0)


class PaperIntentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str | None = None
    symbol: str | None = None
    direction: Literal["long", "short"] | None = None
    reference_price: float | None = Field(None, gt=0)
    quantity: float | None = Field(None, gt=0)
    risk_dollars: float | None = Field(None, gt=0)
    entry_model: EntryModelName = "spread_plus_slippage"
    slippage_bps: float = Field(2.0, ge=0, le=500)
    stop: StopModel = Field(default_factory=StopModel)
    target: TargetModel = Field(default_factory=TargetModel)
    time_exit_minutes: int | None = Field(60, ge=1, le=390)
    expires_minutes: int = Field(15, ge=1, le=390)
    simulate_fill: bool = True

    @model_validator(mode="after")
    def _v(self) -> PaperIntentRequest:
        if self.event_id is None and (
            self.symbol is None or self.reference_price is None or self.direction is None
        ):
            raise ValueError("provide event_id, or symbol + direction + reference_price")
        if (self.quantity is None) == (self.risk_dollars is None):
            raise ValueError("provide exactly one of quantity or risk_dollars")
        return self


class IntentRejected(ValueError):
    pass


@dataclass
class PaperIntent:
    intent_id: str
    event_id: str | None
    symbol: str
    direction: str
    quantity: float
    entry_model: str
    stop_model: dict[str, Any]
    target_model: dict[str, Any]
    time_exit_minutes: int | None
    est_spread_bps: float
    est_slippage_bps: float
    max_modeled_loss: float
    strategy_id: str | None
    strategy_version: int | None
    status: str
    created_at: str
    expires_at: str
    reference_price: float
    est_entry_price: float
    stop_price: float | None
    target_price: float | None
    detail: dict[str, Any] = field(default_factory=dict)
    simulated: bool = True
    submitted_to_broker: bool = False
    label: str = PAPER_LABEL

    def as_row(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d.pop("simulated"), d.pop("submitted_to_broker"), d.pop("label")
        d["detail"] = {
            **self.detail,
            "reference_price": self.reference_price,
            "est_entry_price": self.est_entry_price,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
        }
        for k in ("reference_price", "est_entry_price", "stop_price", "target_price"):
            d.pop(k)
        return d

    def as_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["assumptions"] = "Hypothetical; spread/slippage are modelled assumptions, not observed executions."
        return d


def slippage_per_share(price: float, bps: float) -> float:
    return price * bps / 10_000


def build_intent(
    req: PaperIntentRequest,
    alert: AlertEvent | None,
    now: datetime | None = None,
    assumed_spread_bps: float = DEFAULT_ASSUMED_SPREAD_BPS,
) -> PaperIntent:
    """Create an unsubmitted intent. Pure function: no I/O, no broker access."""
    now = now or utcnow()
    direction: str
    if alert is not None:
        if alert.status not in ("triggered", "acknowledged"):
            raise IntentRejected(
                f"alert is {alert.status}; only triggered/acknowledged alerts can produce an intent"
            )
        symbol, direction = alert.symbol, req.direction or alert.direction
        ref = req.reference_price or alert.trigger_price
        spread_bps = alert.spread_bps if alert.spread_bps is not None else assumed_spread_bps
        atr = alert.feature_snapshot.get("atr")
        strat_id, strat_ver = alert.strategy_id, alert.strategy_version
        spread_assumed = alert.spread_bps is None
    else:
        symbol, direction, ref = str(req.symbol).upper(), str(req.direction), req.reference_price
        spread_bps, atr, strat_id, strat_ver, spread_assumed = assumed_spread_bps, None, None, None, True
    if not ref or ref <= 0:
        raise IntentRejected("no usable reference price")
    is_long = direction == "long"
    sgn = 1 if is_long else -1

    half_spread = ref * spread_bps / 20_000
    slip = slippage_per_share(ref, req.slippage_bps)
    if req.entry_model in ("next_trade", "next_bar_open"):
        est_entry, est_slip = ref, 0.0
    elif req.entry_model == "bid_ask_cross":
        est_entry, est_slip = ref + sgn * half_spread, 0.0
    elif req.entry_model == "fixed_slippage":
        est_entry, est_slip = ref + sgn * slip, slip
    else:  # spread_plus_slippage
        est_entry, est_slip = ref + sgn * (half_spread + slip), slip

    if req.stop.type == "none":
        stop_dist = 0.0
    elif req.stop.type == "percent":
        stop_dist = est_entry * req.stop.value / 100
    elif req.stop.type == "dollars":
        stop_dist = req.stop.value
    else:  # atr
        if not isinstance(atr, int | float) or atr <= 0:
            raise IntentRejected("ATR stop requires an alert with an ATR feature value")
        stop_dist = float(atr) * req.stop.value
    stop_price = round(est_entry - sgn * stop_dist, 4) if stop_dist else None
    if req.target.type == "none":
        target_price = None
    elif req.target.type == "percent":
        target_price = round(est_entry + sgn * est_entry * req.target.value / 100, 4)
    elif req.target.type == "dollars":
        target_price = round(est_entry + sgn * req.target.value, 4)
    else:
        if not stop_dist:
            raise IntentRejected("r_multiple target requires a stop")
        target_price = round(est_entry + sgn * stop_dist * req.target.value, 4)

    per_share_risk = stop_dist + 2 * est_slip + 2 * half_spread
    if req.quantity is not None:
        qty = math.floor(req.quantity)
    else:
        if per_share_risk <= 0:
            raise IntentRejected("risk-based sizing requires a stop")
        qty = math.floor(float(req.risk_dollars) / per_share_risk)  # type: ignore[arg-type]
    if qty < 1:
        raise IntentRejected("computed quantity is below 1 share")
    max_loss = qty * per_share_risk if stop_dist else qty * est_entry  # no stop: whole notional at risk

    return PaperIntent(
        intent_id="pi_" + uuid.uuid4().hex[:16],
        event_id=alert.event_id if alert else None,
        symbol=symbol,
        direction=direction,
        quantity=float(qty),
        entry_model=req.entry_model,
        stop_model=req.stop.model_dump(),
        target_model=req.target.model_dump(),
        time_exit_minutes=req.time_exit_minutes,
        est_spread_bps=round(spread_bps, 3),
        est_slippage_bps=req.slippage_bps,
        max_modeled_loss=round(max_loss, 2),
        strategy_id=strat_id,
        strategy_version=strat_ver,
        status="pending",
        created_at=iso(now) or "",
        expires_at=iso(now + timedelta(minutes=req.expires_minutes)) or "",
        reference_price=ref,
        est_entry_price=round(est_entry, 4),
        stop_price=stop_price,
        target_price=target_price,
        detail={
            "spread_assumed": spread_assumed,
            "max_modeled_loss_formula": "qty * (stop_distance + 2*slippage + spread)",
            "per_share_risk": round(per_share_risk, 6),
        },
    )


# =================================================================================== fill simulation
@dataclass
class MarketSnapshot:
    as_of: datetime
    bid: float | None = None
    ask: float | None = None
    bid_size: float = 0.0
    ask_size: float = 0.0
    last: float | None = None
    next_trade_price: float | None = None
    next_trade_size: float = 0.0
    next_bar_open: float | None = None
    next_bar_volume: float = 0.0
    data_age_seconds: float = 0.0


@dataclass
class FillConfig:
    max_data_age_seconds: float = 15.0
    participation_rate: float = 0.10  # fraction of next-bar volume we may take
    min_fill_fraction: float = 0.25  # below this the whole order is rejected
    allow_partial: bool = True
    slippage_bps: float = 2.0
    fixed_slippage_cents: float | None = None  # overrides slippage_bps when set


@dataclass
class SimulatedFill:
    fill_id: str
    intent_id: str
    symbol: str
    side: str
    quantity: float
    price: float | None
    fill_ts: str
    model: str
    partial: bool
    status: str  # filled | partial | rejected
    reject_reason: str | None
    detail: dict[str, Any]
    simulated: bool = True

    def as_row(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d.pop("simulated")
        return d


def _adverse(price: float, side: str, cfg: FillConfig) -> float:
    per_share = (
        cfg.fixed_slippage_cents / 100
        if cfg.fixed_slippage_cents is not None
        else slippage_per_share(price, cfg.slippage_bps)
    )
    return price + per_share if side == "buy" else price - per_share


def simulate_fill(
    intent_id: str,
    symbol: str,
    side: Literal["buy", "sell"],
    quantity: float,
    model: str,
    snap: MarketSnapshot,
    cfg: FillConfig | None = None,
) -> SimulatedFill:
    """Deterministic simulated fill. Rejects on stale data or insufficient liquidity."""
    cfg = cfg or FillConfig()
    fid = "pf_" + uuid.uuid4().hex[:16]

    def reject(reason: str) -> SimulatedFill:
        return SimulatedFill(
            fid,
            intent_id,
            symbol,
            side,
            0.0,
            None,
            iso(snap.as_of) or "",
            model,
            False,
            "rejected",
            reason,
            {"snapshot_age_s": snap.data_age_seconds},
        )

    if snap.data_age_seconds > cfg.max_data_age_seconds:
        return reject(f"stale data: {snap.data_age_seconds:.1f}s > {cfg.max_data_age_seconds:.1f}s")
    price: float | None
    avail: float
    if model == "next_trade":
        price, avail = snap.next_trade_price, snap.next_trade_size
    elif model == "next_bar_open":
        price, avail = snap.next_bar_open, snap.next_bar_volume * cfg.participation_rate
    elif model == "bid_ask_cross":
        price = snap.ask if side == "buy" else snap.bid
        avail = snap.ask_size if side == "buy" else snap.bid_size
    elif model == "fixed_slippage":
        ref = snap.last
        price = _adverse(ref, side, cfg) if ref else None
        avail = snap.ask_size if side == "buy" else snap.bid_size
    elif model == "spread_plus_slippage":
        touch = snap.ask if side == "buy" else snap.bid
        price = _adverse(touch, side, cfg) if touch else None
        avail = snap.ask_size if side == "buy" else snap.bid_size
    else:
        return reject(f"unknown entry model {model!r}")
    if price is None or price <= 0:
        return reject(f"no market data available for model {model}")
    if model in ("fixed_slippage", "spread_plus_slippage") and snap.next_bar_volume:
        avail = max(avail, snap.next_bar_volume * cfg.participation_rate)
    avail = math.floor(avail)
    if avail <= 0:
        return reject("insufficient liquidity: nothing available at the modelled price")
    fill_qty = min(quantity, float(avail))
    if fill_qty < quantity * cfg.min_fill_fraction or (fill_qty < quantity and not cfg.allow_partial):
        return reject(f"insufficient liquidity: {avail:.0f} available for {quantity:.0f} requested")
    partial = fill_qty < quantity
    return SimulatedFill(
        fid,
        intent_id,
        symbol,
        side,
        fill_qty,
        round(price, 4),
        iso(snap.as_of) or "",
        model,
        partial,
        "partial" if partial else "filled",
        None,
        {
            "requested": quantity,
            "available": avail,
            "unfilled_cancelled": quantity - fill_qty,
            "note": "SIMULATED - never sent to a broker",
        },
    )


# ================================================================================= exits & positions
def resolve_stop_target(
    direction: str,
    stop: float | None,
    target: float | None,
    bar: Bar,
    policy: IntrabarPolicy,
) -> tuple[str, float] | Literal["ambiguous"] | None:
    """Decide which level a 1-minute OHLC bar hit. Gap-through fills at the open.

    Returns (reason, price), ``"ambiguous"`` (policy reject_ambiguous) or None when nothing was touched.
    A 1-minute bar cannot reveal the intrabar order of high and low, hence the explicit policy.
    """
    is_long = direction == "long"
    stop_gap = stop is not None and ((bar.open <= stop) if is_long else (bar.open >= stop))
    tgt_gap = target is not None and ((bar.open >= target) if is_long else (bar.open <= target))
    if stop_gap and tgt_gap:  # impossible unless stop/target are inverted; be conservative
        return "stop", bar.open
    if stop_gap:
        return "stop", bar.open
    if tgt_gap:
        return "target", bar.open
    stop_hit = stop is not None and ((bar.low <= stop) if is_long else (bar.high >= stop))
    tgt_hit = target is not None and ((bar.high >= target) if is_long else (bar.low <= target))
    if stop_hit and tgt_hit:
        if policy == "stop_first":
            return "stop", float(stop)  # type: ignore[arg-type]
        if policy == "target_first":
            return "target", float(target)  # type: ignore[arg-type]
        return "ambiguous"
    if stop_hit:
        return "stop", float(stop)  # type: ignore[arg-type]
    if tgt_hit:
        return "target", float(target)  # type: ignore[arg-type]
    return None


@dataclass
class PaperPosition:
    symbol: str
    direction: str
    quantity: float
    avg_entry: float
    intent_id: str
    opened_at: str
    stop_price: float | None
    target_price: float | None
    time_exit_at: str | None
    status: str = "open"
    exit_price: float | None = None
    exit_reason: str | None = None
    closed_at: str | None = None
    realized_pnl: float | None = None
    simulated: bool = True

    def unrealized(self, mark: float | None) -> float | None:
        if mark is None or self.status != "open":
            return None
        sgn = 1 if self.direction == "long" else -1
        return round(sgn * (mark - self.avg_entry) * self.quantity, 2)

    def as_dict(self, mark: float | None = None) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["mark"] = mark
        d["unrealized_pnl"] = self.unrealized(mark)
        d["label"] = "SIMULATED POSITION - NOT A BROKER POSITION"
        return d
