"""Backtest engine tests on hand-scripted bar paths with hand-computed expected results."""

from __future__ import annotations

import json
from datetime import date

import pytest
from pydantic import ValidationError

from scanalert.backtest import (
    BacktestConfig,
    CostConfig,
    ExitConfig,
    LevelSpec,
    PointInTimeUniverse,
    SizingConfig,
    StaticUniverse,
    run_backtest,
)
from scanalert.calendar import NyseCalendar
from scanalert.filters import FilterSpec
from scanalert.models import Bar
from scanalert.strategy import AlertCondition, StrategySpec

CAL = NyseCalendar()
HIST = [date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24), date(2026, 9, 25)]
D1, D2 = date(2026, 9, 28), date(2026, 9, 29)
ZERO = CostConfig(spread_bps=0, slippage_bps=0)
ALERT_IDX = 10  # bar 09:40 closes at 10.5 -> alert; entry fills at the NEXT bar (idx 11) open


def flt(id_, field, op, value, **kw):
    return FilterSpec(id=id_, name=id_, field=field, operator=op, value=value, **kw)


def spec(threshold=10.4, cooldown=0) -> StrategySpec:
    return StrategySpec(
        id="bt",
        name="bt",
        filters=[flt("g", "last", "gte", 5)],
        alert_conditions=[
            AlertCondition(
                id="c",
                name="c",
                event_type="breakout",
                filters=[flt("px", "last", "gt", threshold)],
                cooldown_seconds=cooldown,
                dedupe_bucket_seconds=1,
                max_alerts_per_symbol_per_day=100,
            )
        ],
    )


def path(sym, days_over: dict[date, dict[int, tuple]], hist=HIST, base=10.0):
    """History flat at `base`; on listed days override bars by minute index with (o,h,l,c[,v])."""
    bars = []
    for d in [*hist, *days_over]:
        for i, m in enumerate(CAL.regular_minutes(d)):
            o = days_over.get(d, {}).get(i)
            bars.append(
                Bar(sym, m, *(o if o else (base, base, base, base)), *(() if o and len(o) > 4 else (1000,)))
            )
    return sorted(bars, key=lambda b: b.ts)


ENTRY = {ALERT_IDX: (10.0, 10.5, 10.0, 10.5)}


def flat_after(i0, price=10.5, n=390):
    return {i: (price, price, price, price) for i in range(i0, n)}


def day(over):
    d = {**ENTRY, **flat_after(11)}
    d.update(over)
    return {D1: d}


def cfg(**kw):
    base = dict(
        start_date=D1,
        end_date=kw.pop("end_date", D1),
        costs=ZERO,
        sizing=SizingConfig(mode="fixed_shares", shares=100),
        exits=ExitConfig(
            profit_target=LevelSpec(type="percent", value=2), stop_loss=LevelSpec(type="percent", value=1)
        ),
    )
    base.update(kw)
    return BacktestConfig(**base)


def run(over, c=None, s=None, sym="AAA", extra=None, **kw):
    bars = {sym: path(sym, over)}
    if extra:
        bars.update(extra)
    return run_backtest(s or spec(), c or cfg(**kw), bars, CAL, provider="fixture", feed="synthetic")


def test_entry_fills_at_next_bar_open_not_alert_close():
    over = day({11: (10.6, 10.6, 10.6, 10.6)})
    r = run(over, exits=ExitConfig(time_after_entry_minutes=3))
    t = r.trades[0]
    assert t["entry_ref_price"] == 10.6 and t["entry_ts"].startswith("2026-09-28T13:41")  # 09:41 ET bar open
    assert t["exit_reason"] == "time_exit"


def test_alert_close_entry_model_is_optimistic_and_labelled():
    r = run(day({}), entry_price_model="alert_close", exits=ExitConfig(time_after_entry_minutes=3))
    assert r.trades[0]["entry_ref_price"] == 10.5
    assert any("optimistic" in a for a in r.report["assumptions"])


def test_target_hit_pnl_hand_computed():
    over = day({12: (10.5, 10.8, 10.5, 10.7)})
    t = run(over).trades[0]
    assert t["exit_reason"] == "profit_target" and t["exit_ref_price"] == pytest.approx(10.71)
    assert t["gross_pnl"] == pytest.approx((10.71 - 10.5) * 100) and t["net_pnl"] == pytest.approx(
        t["gross_pnl"]
    )


def test_stop_hit_pnl():
    t = run(day({12: (10.5, 10.5, 10.3, 10.4)})).trades[0]
    assert (
        t["exit_reason"] == "stop_loss"
        and t["exit_ref_price"] == pytest.approx(10.395)
        and t["net_pnl"] == pytest.approx(-10.5)
    )


@pytest.mark.parametrize(
    ("policy", "reason", "net"), [("stop_first", "stop_loss", -10.5), ("target_first", "profit_target", 21.0)]
)
def test_intrabar_ambiguity_policy_changes_outcome_and_is_reported(policy, reason, net):
    r = run(day({12: (10.5, 10.8, 10.3, 10.5)}), intrabar_policy=policy)
    assert r.trades[0]["exit_reason"] == reason and r.trades[0]["net_pnl"] == pytest.approx(net)
    assert r.report["intrabar_policy"] == policy and any(policy in a for a in r.report["assumptions"])


def test_reject_ambiguous_excludes_trade_and_counts_it():
    r = run(day({12: (10.5, 10.8, 10.3, 10.5)}), intrabar_policy="reject_ambiguous")
    assert r.trades == [] and r.report["metrics"]["ambiguous_bars_excluded"] == 1
    assert r.report["skipped_signals"]["trade excluded: ambiguous stop/target bar"] == 1


def test_gap_through_stop_fills_at_open():
    t = run(day({12: (10.2, 10.25, 10.1, 10.2)})).trades[0]
    assert (
        t["exit_reason"] == "stop_loss"
        and t["exit_ref_price"] == 10.2
        and t["net_pnl"] == pytest.approx(-30.0)
    )


def test_stop_and_target_can_hit_in_the_entry_bar():
    over = day({11: (10.5, 10.5, 10.3, 10.4)})
    t = run(over).trades[0]
    assert t["exit_reason"] == "stop_loss" and t["exit_ts"] == t["entry_ts"]


def test_trailing_stop_uses_prior_high_water_mark():
    c = cfg(exits=ExitConfig(trailing_stop=LevelSpec(type="percent", value=1)))
    over = day({12: (10.5, 11.0, 10.5, 10.95), 13: (10.95, 10.95, 10.85, 10.9)})
    t = run(over, c).trades[0]
    assert t["exit_reason"] == "trailing_stop" and t["exit_ref_price"] == pytest.approx(11.0 * 0.99)
    assert t["net_pnl"] == pytest.approx((10.89 - 10.5) * 100)


def test_trailing_stop_does_not_use_same_bar_extreme():
    c = cfg(exits=ExitConfig(trailing_stop=LevelSpec(type="percent", value=1)), intrabar_policy="stop_first")
    over = day(
        {12: (10.5, 11.0, 10.3, 10.6)}
    )  # spike then drop inside ONE bar: trail was set from entry (10.395)
    t = run(over, c).trades[0]
    assert t["exit_ref_price"] == pytest.approx(10.5 * 0.99)


def test_time_after_entry_exit_at_open_of_target_bar():
    over = day({16: (10.7, 10.7, 10.7, 10.7)})
    t = run(over, exits=ExitConfig(time_after_entry_minutes=5)).trades[0]
    assert t["exit_reason"] == "time_exit" and t["exit_ref_price"] == 10.7 and t["holding_minutes"] == 5


def test_time_of_day_exit():
    over = day({i: (10.8, 10.8, 10.8, 10.8) for i in range(90, 390)})  # 11:00 ET = idx 90
    t = run(over, exits=ExitConfig(time_of_day_exit="11:00")).trades[0]
    assert (
        t["exit_reason"] == "time_of_day_exit"
        and t["exit_ref_price"] == 10.8
        and t["exit_ts"].startswith("2026-09-28T15:00")
    )


def test_same_day_close_fallback():
    over = day({389: (10.6, 10.6, 10.6, 10.6)})
    t = run(over, exits=ExitConfig()).trades[0]
    assert (
        t["exit_reason"] == "eod_close"
        and t["exit_ref_price"] == 10.6
        and t["exit_ts"].startswith("2026-09-28T20:00")
    )


def test_early_close_day_uses_early_session_end():
    d = date(2026, 11, 27)
    hist = [date(2026, 11, 20), date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 25)]
    n = len(CAL.regular_minutes(d))
    assert n == 210
    over = {d: {ALERT_IDX: (10.0, 10.5, 10.0, 10.5), **{i: (10.5, 10.5, 10.5, 10.5) for i in range(11, n)}}}
    bars = {"AAA": path("AAA", over, hist=hist)}
    r = run_backtest(spec(), cfg(start_date=d, end_date=d, exits=ExitConfig()), bars, CAL)
    assert r.trades[0]["exit_reason"] == "eod_close" and r.trades[0]["exit_ts"].startswith(
        "2026-11-27T18:00"
    )  # 13:00 ET


@pytest.mark.parametrize(
    ("mode", "reason", "hour"),
    [("close", "multi_day_close", "2026-09-29T20:00"), ("open", "multi_day_open", "2026-09-29T13:30")],
)
def test_next_day_exits(mode, reason, hour):
    over = {
        **day({}),
        D2: {0: (10.9, 10.9, 10.9, 10.9), **{i: (10.8, 10.8, 10.8, 10.8) for i in range(1, 390)}},
    }
    t = run(over, end_date=D2, exits=ExitConfig(hold_days=1, multi_day_exit=mode)).trades[0]
    assert t["exit_reason"] == reason and t["exit_ts"].startswith(hour)
    assert t["exit_ref_price"] == (10.9 if mode == "open" else 10.8)


def test_overnight_gap_through_stop_on_next_day():
    over = {**day({}), D2: {i: (9.8, 9.9, 9.7, 9.8) for i in range(0, 390)}}
    t = run(
        over, end_date=D2, exits=ExitConfig(hold_days=1, stop_loss=LevelSpec(type="percent", value=1))
    ).trades[0]
    assert (
        t["exit_reason"] == "stop_loss"
        and t["exit_ref_price"] == 9.8
        and t["exit_ts"].startswith("2026-09-29T13:30")
    )


def test_data_end_flatten_reported():
    over = {**day({})}
    t = run(over, exits=ExitConfig(hold_days=3)).trades[0]
    assert t["exit_reason"] == "data_end"


def test_alert_based_exit_next_bar_open():
    exit_f = [flt("x", "last", "lt", 10.45)]
    over = day({20: (10.4, 10.4, 10.4, 10.4), **{i: (10.4, 10.4, 10.4, 10.4) for i in range(21, 390)}})
    t = run(over, exits=ExitConfig(exit_filters=exit_f)).trades[0]
    assert t["exit_reason"] == "alert_exit" and t["exit_ref_price"] == 10.4


def test_one_entry_per_symbol_per_day_and_reentry_allowed_when_disabled():
    over = day(
        {
            20: (10.0, 10.0, 10.0, 10.0),
            21: (10.0, 10.0, 10.0, 10.0),
            22: (10.6, 10.6, 10.6, 10.6),
            **{i: (10.6, 10.6, 10.6, 10.6) for i in range(23, 390)},
        }
    )
    e = ExitConfig(time_after_entry_minutes=3)
    once = run(over, exits=e)
    assert len(once.trades) == 1 and once.report["skipped_signals"]["one entry per symbol per day"] == 1
    multi = run(over, exits=e, one_entry_per_symbol_per_day=False)
    assert len(multi.trades) == 2


def test_daily_trade_cap_and_max_concurrent():
    syms = ["AAA", "BBB", "CCC"]
    bars = {s: path(s, day({})) for s in syms}
    c = cfg(exits=ExitConfig(), max_trades_per_day=2, symbols=syms)
    r = run_backtest(spec(), c, bars, CAL)
    assert len(r.trades) == 2 and r.report["skipped_signals"]["daily trade cap"] == 1
    c2 = cfg(exits=ExitConfig(), max_concurrent_positions=1, symbols=syms)
    r2 = run_backtest(spec(), c2, bars, CAL)
    assert len(r2.trades) == 1 and r2.report["skipped_signals"]["max concurrent positions"] == 2


def test_entry_window_blocks_early_and_late_signals():
    early = {i: (10.0, 10.0, 10.0, 10.0) for i in range(0, 390)}
    early[2] = (10.0, 10.6, 10.0, 10.6)  # 09:32 alert, before 09:35 window start
    for i in range(3, 390):
        early[i] = (10.6, 10.6, 10.6, 10.6)
    r = run({D1: early}, exits=ExitConfig())
    assert r.trades == [] and r.report["skipped_signals"]["outside entry window"] == 1


def test_daily_loss_limit_blocks_new_entries_after_losses():
    syms = ["AAA", "BBB"]
    loser = {**day({12: (10.5, 10.5, 10.3, 10.4)})}  # AAA stops out for -10.5
    late = {
        ALERT_IDX + 30: (10.0, 10.6, 10.0, 10.6),
        **{i: (10.6, 10.6, 10.6, 10.6) for i in range(ALERT_IDX + 31, 390)},
    }
    b_over = {D1: {i: (10.0, 10.0, 10.0, 10.0) for i in range(0, ALERT_IDX + 30)} | late}
    bars = {"AAA": path("AAA", loser), "BBB": path("BBB", b_over)}
    open_limit = run_backtest(
        spec(),
        cfg(symbols=syms, exits=ExitConfig(stop_loss=LevelSpec(type="percent", value=1)), daily_loss_limit=5),
        bars,
        CAL,
    )
    assert [t["symbol"] for t in open_limit.trades] == ["AAA"] and open_limit.report["skipped_signals"][
        "daily loss limit"
    ] == 1
    no_limit = run_backtest(
        spec(), cfg(symbols=syms, exits=ExitConfig(stop_loss=LevelSpec(type="percent", value=1))), bars, CAL
    )
    assert len(no_limit.trades) == 2


@pytest.mark.parametrize(
    ("sizing", "qty"),
    [
        (SizingConfig(mode="fixed_shares", shares=37), 37),
        (SizingConfig(mode="fixed_dollars", dollars=1000), 95),  # floor(1000/10.5)
        (SizingConfig(mode="percent_equity", percent=2), 190),  # 2% of 100k = 2000 / 10.5
        (
            SizingConfig(mode="risk_percent", risk_percent=0.5),
            500 // 1 and int(500 / (10.5 * 0.01)),
        ),  # $500 risk / $0.105 stop
    ],
)
def test_position_sizing_modes(sizing, qty):
    over = day({})
    t = run(over, sizing=sizing, exits=ExitConfig(stop_loss=LevelSpec(type="percent", value=1))).trades[0]
    assert t["quantity"] == qty


def test_buying_power_caps_position_and_records_peak():
    over = day({})
    r = run(
        over,
        sizing=SizingConfig(mode="fixed_shares", shares=100_000),
        starting_equity=10_000,
        leverage=1.0,
        exits=ExitConfig(),
    )
    t = r.trades[0]
    assert t["quantity"] == int(10_000 / 10.5) and r.report["metrics"]["buying_power_peak"] <= 10_000
    assert r.report["skipped_signals"]["position reduced to fit buying power"] == 1
    lev = run(
        over,
        sizing=SizingConfig(mode="fixed_shares", shares=100_000),
        starting_equity=10_000,
        leverage=2.0,
        exits=ExitConfig(),
    )
    assert lev.trades[0]["quantity"] == int(20_000 / 10.5)


def test_costs_spread_slippage_commission_hand_computed():
    costs = CostConfig(
        commission_per_share=0.01, min_commission_per_order=1.0, spread_bps=20, slippage_bps=10
    )
    over = day({12: (10.5, 10.8, 10.5, 10.7)})  # target 2% hit (limit fill: no spread/slip on exit)
    t = run(over, costs=costs, exits=ExitConfig(profit_target=LevelSpec(type="percent", value=2))).trades[0]
    # entry buy at 10.5 + half spread (0.0105) + slip (0.0105); fill 10.5210, target computed from the FILL
    fill = 10.5 + 10.5 * 20 / 20000 + 10.5 * 10 / 10000
    assert t["entry_price"] == pytest.approx(fill, abs=1e-4)
    assert t["spread_cost"] == pytest.approx(10.5 * 20 / 20000 * 100) and t["slippage_cost"] == pytest.approx(
        10.5 * 10 / 10000 * 100
    )
    assert t["commission"] == pytest.approx(max(1.0, 100 * 0.01) * 2)
    assert t["net_pnl"] == pytest.approx(
        t["gross_pnl"] - t["spread_cost"] - t["slippage_cost"] - t["commission"]
    )


def test_short_direction_override_pnl_sign():
    over = day({12: (10.5, 10.5, 10.2, 10.3)})
    t = run(
        over, direction="short", exits=ExitConfig(profit_target=LevelSpec(type="percent", value=2))
    ).trades[0]
    assert t["direction"] == "short" and t["exit_reason"] == "profit_target" and t["net_pnl"] > 0


def test_report_contains_every_required_metric_and_assumption():
    r = run(day({12: (10.5, 10.8, 10.5, 10.7)}))
    rep = r.report
    for k in (
        "total_trades",
        "win_rate",
        "profit_factor",
        "expectancy",
        "average_winner",
        "average_loser",
        "gross_pnl",
        "net_pnl",
        "max_drawdown",
        "buying_power_peak",
        "max_consecutive_wins",
        "max_consecutive_losses",
        "total_commissions_fees",
        "total_spread_cost",
        "total_slippage_cost",
    ):
        assert k in rep["metrics"], k
    for k in (
        "equity_curve",
        "daily_pnl",
        "trades_per_day",
        "holding_time",
        "exit_reasons",
        "attribution",
        "data",
        "strategy",
        "assumptions",
        "intrabar_policy",
        "label",
        "config",
    ):
        assert k in rep, k
    assert (
        rep["data"]["provider"] == "fixture"
        and rep["data"]["feed"] == "synthetic"
        and rep["data"]["requested_range"] == ["2026-09-28", "2026-09-28"]
    )
    assert rep["data"]["per_symbol"]["AAA"]["coverage_pct"] == 100.0
    assert "HISTORICAL SIMULATION" in rep["label"] and "not a prediction" in rep["label"].lower()
    assert rep["strategy"]["config_hash"] == spec().config_snapshot()["config_hash"]
    assert rep["holding_time"]["distribution"] and rep["attribution"]["by_filter"]
    assert rep["equity_curve"][0]["equity"] == 100_000.0


def test_profit_factor_expectancy_streaks_and_drawdown():
    syms = ["AAA", "BBB", "CCC"]
    win = day({12: (10.5, 10.8, 10.5, 10.7)})
    lose = day({12: (10.5, 10.5, 10.3, 10.4)})
    bars = {"AAA": path("AAA", win), "BBB": path("BBB", lose), "CCC": path("CCC", lose)}
    r = run_backtest(spec(), cfg(symbols=syms, max_concurrent_positions=3), bars, CAL)
    m = r.report["metrics"]
    assert m["total_trades"] == 3 and m["winners"] == 1 and m["losers"] == 2
    assert m["win_rate"] == pytest.approx(1 / 3, abs=1e-3)
    assert m["profit_factor"] == pytest.approx(21.0 / 21.0)
    assert m["expectancy"] == pytest.approx(0.0, abs=1e-6)
    assert m["average_winner"] == pytest.approx(21.0) and m["average_loser"] == pytest.approx(-10.5)
    assert m["max_consecutive_losses"] >= 1 and m["max_drawdown"] > 0
    assert sum(d["net_pnl"] for d in r.report["daily_pnl"]) == pytest.approx(m["net_pnl"])


def test_no_trades_report_is_valid():
    r = run({D1: {i: (10.0, 10.0, 10.0, 10.0) for i in range(390)}})
    assert (
        r.report["metrics"]["total_trades"] == 0
        and r.report["metrics"]["win_rate"] is None
        and r.report["metrics"]["profit_factor_note"]
    )


def test_backtest_is_deterministic():
    over = day({12: (10.5, 10.8, 10.5, 10.7)})
    a = run(over)
    b = run(over)
    assert json.dumps(a.trades, sort_keys=True) == json.dumps(b.trades, sort_keys=True)
    assert json.dumps(a.report, sort_keys=True, default=str) == json.dumps(
        b.report, sort_keys=True, default=str
    )


def test_same_scanner_code_as_live(fx):
    """Backtest signals equal what the live EventScanner emits on the same bars (shared logic)."""
    from scanalert.features import SymbolState
    from scanalert.scanner import EventScanner
    from scanalert.strategy import opening_range_breakout

    s = opening_range_breakout()
    d = fx.days[-1]
    cfg_ = BacktestConfig(
        start_date=d, end_date=d, exits=ExitConfig(), assumed_spread_bps_by_symbol=fx.spread_bps
    )
    bt = run_backtest(s, cfg_, {"ORBX": fx.bars["ORBX"]}, CAL)
    live_state, sc = SymbolState("ORBX", CAL), EventScanner(CAL)
    from datetime import timedelta

    from scanalert.models import Quote

    live_signals = []
    for b in sorted(fx.bars["ORBX"], key=lambda x: x.ts):
        live_state.on_bar(b)
        if CAL.session_at(b.ts) != "regular" or b.ts < CAL.open_dt(d):
            continue
        as_of = b.ts + timedelta(minutes=1)
        half = max(0.005, b.close * fx.spread_bps["ORBX"] / 20000)
        live_state.on_quote(Quote("ORBX", as_of, round(b.close - half, 4), round(b.close + half, 4)))
        live_signals += sc.evaluate(s, live_state, as_of, {"ORBX"}) if b.ts.date() == d or True else []
    # signals on the test day only
    day_signals = [x for x in live_signals if x.as_of >= CAL.open_dt(d)]
    assert bt.report["metrics"]["signals_detected"] >= 1
    assert bt.report["metrics"]["signals_detected"] == len(day_signals)


def test_universe_point_in_time_and_delisting():
    u = PointInTimeUniverse(
        {date(2026, 9, 1): ["AAA"], date(2026, 9, 28): ["AAA", "BBB"]}, delisted={"AAA": date(2026, 9, 29)}
    )
    assert u.symbols_on(date(2026, 8, 31)) == []
    assert u.symbols_on(date(2026, 9, 10)) == ["AAA"]
    assert u.symbols_on(date(2026, 9, 28)) == ["AAA", "BBB"]
    assert u.symbols_on(date(2026, 9, 30)) == ["BBB"]
    assert StaticUniverse(["B", "A"]).symbols_on(D1) == ["A", "B"]


def test_backtest_respects_point_in_time_membership():
    bars = {"AAA": path("AAA", day({})), "BBB": path("BBB", day({}))}
    pit = PointInTimeUniverse({D1: ["AAA"]})
    r = run_backtest(spec(), cfg(exits=ExitConfig(), symbols=["AAA", "BBB"]), bars, CAL, universe=pit)
    assert [t["symbol"] for t in r.trades] == ["AAA"]
    late = PointInTimeUniverse({date(2026, 9, 30): ["BBB"]})
    r2 = run_backtest(spec(), cfg(exits=ExitConfig(), symbols=["BBB"]), bars, CAL, universe=late)
    assert r2.trades == []


def test_holiday_and_weekend_dates_are_not_simulated():
    c = cfg(start_date=date(2026, 9, 5), end_date=date(2026, 9, 7), exits=ExitConfig())
    r = run_backtest(spec(), c, {"AAA": path("AAA", {})}, CAL)
    assert r.report["data"]["trading_days_in_range"] == 0 and r.report["metrics"]["total_trades"] == 0


def test_coverage_reports_missing_bars():
    bars = path("AAA", day({}))
    kept = [b for i, b in enumerate(bars) if not (b.ts.date() == D1 and 100 <= i % 390 < 110 and False)]
    kept = [
        b for b in bars if not (b.ts.date() == D1 and CAL.regular_minutes(D1).index(b.ts) in range(100, 110))
    ]
    r = run_backtest(spec(), cfg(exits=ExitConfig()), {"AAA": kept}, CAL)
    cov = r.report["data"]["per_symbol"]["AAA"]
    assert (
        cov["bars"] == 380
        and cov["expected"] == 390
        and cov["coverage_pct"] == pytest.approx(97.44, abs=0.01)
    )


def test_config_validation():
    with pytest.raises(ValidationError, match="end_date"):
        BacktestConfig(start_date=D2, end_date=D1)
    with pytest.raises(ValidationError, match="extended-hours"):
        BacktestConfig(start_date=D1, end_date=D1, include_premarket=True)
    with pytest.raises(ValidationError, match="stop_loss"):
        BacktestConfig(start_date=D1, end_date=D1, sizing=SizingConfig(mode="risk_percent"))
    with pytest.raises(ValidationError, match="entry_start"):
        BacktestConfig(start_date=D1, end_date=D1, entry_start="15:00", entry_end="10:00")
    with pytest.raises(ValidationError):
        ExitConfig(time_of_day_exit="noon")
    with pytest.raises(ValidationError):
        BacktestConfig(start_date=D1, end_date=D1, mystery=1)


def test_synthetic_quotes_make_spread_filters_effective():
    s = StrategySpec(
        id="sp",
        name="sp",
        filters=[flt("g", "last", "gte", 5), flt("sp", "spread_bps", "lte", 50, null_policy="pass")],
        alert_conditions=[
            AlertCondition(id="c", name="c", filters=[flt("px", "last", "gt", 10.4)], cooldown_seconds=0)
        ],
    )
    over = day({})
    no_quotes = run_backtest(s, cfg(exits=ExitConfig()), {"AAA": path("AAA", over)}, CAL)
    wide = run_backtest(s, cfg(exits=ExitConfig(), assumed_spread_bps=150), {"AAA": path("AAA", over)}, CAL)
    assert len(no_quotes.trades) == 1 and wide.trades == []
    assert any("Quotes: none" in a for a in no_quotes.report["assumptions"])
