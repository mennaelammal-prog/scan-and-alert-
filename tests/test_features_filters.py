from __future__ import annotations

from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from conftest import flat_day, mkbar
from scanalert.calendar import NyseCalendar
from scanalert.features import FEATURES, FeatureContext, SymbolState, compute_all
from scanalert.filters import EvalContext, FilterSpec, evaluate_all, evaluate_filter
from scanalert.models import Quote

CAL = NyseCalendar()
D0, D1, D2, D3, D4 = (
    date(2026, 9, 21),
    date(2026, 9, 22),
    date(2026, 9, 23),
    date(2026, 9, 24),
    date(2026, 9, 25),
)
TODAY = date(2026, 9, 28)


def state_with_history(prior_vol=1000.0, days=(D0, D1, D2, D3, D4), price=10.0) -> SymbolState:
    st = SymbolState("TST", CAL)
    for d in days:
        st.seed_history(flat_day("TST", d, price, prior_vol, CAL))
    return st


def feed(st, bars):
    for b in bars:
        st.on_bar(b)
    last = bars[-1]
    return last.ts + timedelta(minutes=1)


def F(**kw):
    base = dict(id="f", name="f", operator="gt", value=0)
    base.update(kw)
    return FilterSpec(**base)


def test_relative_volume_ratio_matches_hand_calculation():
    st = state_with_history(1000)
    mins = CAL.regular_minutes(TODAY)[:10]
    as_of = feed(st, [mkbar("TST", m, 10, 10, 10, 10, 3000) for m in mins])
    assert FeatureContext(st, as_of).get("rvol") == pytest.approx(3.0)


def test_relative_volume_requires_min_prior_days():
    st = state_with_history(1000, days=(D3, D4))
    as_of = feed(st, [mkbar("TST", CAL.regular_minutes(TODAY)[0], 10, 10, 10, 10, 3000)])
    assert FeatureContext(st, as_of).get("rvol") is None


def test_relative_volume_uses_same_minute_baseline_not_full_day():
    st = state_with_history(1000)
    as_of = feed(st, [mkbar("TST", m, 10, 10, 10, 10, 1000) for m in CAL.regular_minutes(TODAY)[:30]])
    assert FeatureContext(st, as_of).get("rvol") == pytest.approx(1.0)


def test_vwap_and_distance():
    st = SymbolState("TST", CAL)
    m = CAL.regular_minutes(TODAY)
    as_of = feed(st, [mkbar("TST", m[0], 10, 11, 9, 10, 100), mkbar("TST", m[1], 12, 13, 11, 12, 300)])
    c = FeatureContext(st, as_of)
    assert c.get("vwap") == pytest.approx((10 * 100 + 12 * 300) / 400)
    assert c.get("vwap_dist_pct") == pytest.approx((12 - 11.5) / 11.5 * 100)


def test_range_features():
    st = state_with_history()
    m = CAL.regular_minutes(TODAY)
    as_of = feed(st, [mkbar("TST", m[0], 10, 12, 9, 11), mkbar("TST", m[1], 11, 11.5, 10, 10.5)])
    c = FeatureContext(st, as_of)
    assert (c.get("day_high"), c.get("day_low"), c.get("range")) == (12, 9, 3)
    assert c.get("range_pct") == pytest.approx(3 / 10.5 * 100)
    assert c.get("range_position") == pytest.approx((10.5 - 9) / 3)
    assert c.get("day_open") == 10


def test_change_vs_previous_close_and_gap():
    st = state_with_history(price=10.0)
    m = CAL.regular_minutes(TODAY)
    as_of = feed(st, [mkbar("TST", m[0], 10.4, 10.6, 10.4, 10.5)])
    c = FeatureContext(st, as_of)
    assert c.get("prev_close") == 10.0
    assert c.get("pct_change") == pytest.approx(5.0)
    assert c.get("dollar_change") == pytest.approx(0.5)
    assert c.get("gap_pct") == pytest.approx(4.0)


def test_spread_features_from_quote():
    st = state_with_history()
    m = CAL.regular_minutes(TODAY)
    as_of = feed(st, [mkbar("TST", m[0], 10, 10, 10, 10)])
    st.on_quote(Quote("TST", as_of, 9.99, 10.01, 100, 200))
    c = FeatureContext(st, as_of)
    assert c.get("spread") == pytest.approx(0.02)
    assert c.get("spread_pct") == pytest.approx(0.2)
    assert c.get("spread_bps") == pytest.approx(20.0)
    assert (c.get("bid_size"), c.get("ask_size")) == (100, 200)


def test_crossed_quote_ignored():
    st = state_with_history()
    st.on_quote(Quote("TST", CAL.open_dt(TODAY), 10.5, 10.0))
    assert st.bid is None


def test_opening_range_breakout_only_after_window_completes():
    st = state_with_history()
    m = CAL.regular_minutes(TODAY)
    bars = [mkbar("TST", m[i], 10, 10.5, 9.5, 10.0) for i in range(14)]
    as_of = feed(st, bars)
    assert FeatureContext(st, as_of).get("orb_high", 15) is None  # only 14 of 15 minutes seen
    as_of = feed(st, [mkbar("TST", m[14], 10, 10.5, 9.5, 10.0)])
    c = FeatureContext(st, as_of)
    assert (c.get("orb_high", 15), c.get("orb_low", 15)) == (10.5, 9.5)
    assert c.get("orb_breakout_up", 15) is False
    as_of = feed(st, [mkbar("TST", m[15], 10.0, 10.9, 10.0, 10.8)])
    c = FeatureContext(st, as_of)
    assert c.get("orb_breakout_up", 15) is True and c.get("orb_breakout_down", 15) is False


def test_new_high_excludes_the_bar_that_made_the_close():
    st = state_with_history()
    m = CAL.regular_minutes(TODAY)
    feed(st, [mkbar("TST", m[i], 10, 10, 10, 10) for i in range(30)])
    # close 10.5 exceeds all previous highs (10) -> new high
    as_of = feed(st, [mkbar("TST", m[30], 10, 11, 10, 10.5)])
    assert FeatureContext(st, as_of).get("new_high", 30) is True
    # own high 11 but close 10 does not exceed previous highs -> not a new high
    st2 = state_with_history()
    feed(st2, [mkbar("TST", m[i], 10, 10, 10, 10) for i in range(30)])
    as_of = feed(st2, [mkbar("TST", m[30], 10, 11, 10, 10)])
    assert FeatureContext(st2, as_of).get("new_high", 30) is False


def test_new_low_and_day_extremes():
    st = state_with_history()
    m = CAL.regular_minutes(TODAY)
    feed(st, [mkbar("TST", m[i], 10, 10.2, 9.9, 10) for i in range(5)])
    as_of = feed(st, [mkbar("TST", m[5], 10, 10, 9.5, 9.6)])
    c = FeatureContext(st, as_of)
    assert c.get("new_day_low") is True and c.get("new_day_high") is False


def test_moving_average_ema_and_distance():
    st = state_with_history()
    m = CAL.regular_minutes(TODAY)
    closes = [10 + i * 0.1 for i in range(20)]
    as_of = feed(st, [mkbar("TST", m[i], c, c + 0.05, c - 0.05, c) for i, c in enumerate(closes)])
    ctx = FeatureContext(st, as_of)
    assert ctx.get("sma", 20) == pytest.approx(sum(closes) / 20)
    assert ctx.get("ma_dist_pct", 20) == pytest.approx(
        (closes[-1] - sum(closes) / 20) / (sum(closes) / 20) * 100
    )
    assert ctx.get("ema", 10) is not None


def test_volatility_zero_for_constant_returns_positive_otherwise():
    st = state_with_history()
    m = CAL.regular_minutes(TODAY)
    as_of = feed(st, [mkbar("TST", m[i], 10, 10, 10, 10) for i in range(25)])
    assert FeatureContext(st, as_of).get("volatility", 20) == pytest.approx(0.0)
    st2 = state_with_history()
    as_of = feed(st2, [mkbar("TST", m[i], 10, 10.2, 9.8, 10 + (0.1 if i % 2 else -0.1)) for i in range(25)])
    assert FeatureContext(st2, as_of).get("volatility", 20) > 0
    assert FeatureContext(st2, as_of).get("atr", 14) > 0


def test_time_of_day_features():
    st = state_with_history()
    m = CAL.regular_minutes(TODAY)
    as_of = feed(st, [mkbar("TST", m[29], 10, 10, 10, 10)])  # bar 10:00-10:01 ET -> as_of 10:00 ET
    c = FeatureContext(st, as_of)
    assert c.get("time_of_day") == pytest.approx(10 * 60)
    assert c.get("minutes_since_open") == pytest.approx(30)
    assert c.get("minutes_to_close") == pytest.approx(360)


def test_liquidity_features():
    st = state_with_history()
    m = CAL.regular_minutes(TODAY)
    as_of = feed(st, [mkbar("TST", m[i], 10, 10, 10, 10, 500) for i in range(4)])
    c = FeatureContext(st, as_of)
    assert c.get("dollar_volume") == pytest.approx(4 * 500 * 10)
    assert c.get("avg_volume", 4) == 500
    assert c.get("vol_sum", 4) == 2000
    assert c.get("avg_dollar_volume", 4) == pytest.approx(5000)


def test_no_lookahead_features_use_only_bars_before_as_of():
    st = state_with_history()
    m = CAL.regular_minutes(TODAY)
    bars = [mkbar("TST", m[i], 10, 10, 10, 10 + i) for i in range(10)]
    for b in bars:
        st.on_bar(b)
    early = bars[4].ts + timedelta(minutes=1)
    assert FeatureContext(st, early).get("sma", 3) == pytest.approx((12 + 13 + 14) / 3)


def test_corrected_bar_replaces_and_recomputes():
    st = state_with_history()
    m = CAL.regular_minutes(TODAY)
    st.on_bar(mkbar("TST", m[0], 10, 10, 10, 10, 100))
    st.on_bar(mkbar("TST", m[1], 10, 10, 10, 10, 100))
    assert st.on_bar(mkbar("TST", m[0], 10, 12, 10, 11, 900, revision=1, corrected=True)) is True
    c = FeatureContext(st, m[1] + timedelta(minutes=1))
    assert c.get("volume") == 1000 and c.get("day_high") == 12
    assert st.corrections_applied == 1
    assert st.on_bar(mkbar("TST", m[0], 10, 10, 10, 10, 100, revision=0)) is False  # stale revision ignored


def test_duplicate_bar_is_noop_and_late_bar_rebuilds():
    st = state_with_history()
    m = CAL.regular_minutes(TODAY)
    b1, b2 = mkbar("TST", m[1], 10, 10, 10, 10, 100), mkbar("TST", m[2], 10, 10, 10, 10, 100)
    st.on_bar(b1)
    assert st.on_bar(b1) is False
    st.on_bar(b2)
    st.on_bar(mkbar("TST", m[0], 10, 10, 10, 10, 100))  # arrives late
    assert st.late_bars == 1 and FeatureContext(st, m[2] + timedelta(minutes=1)).get("volume") == 300


def test_compute_all_covers_registry():
    st = state_with_history()
    as_of = feed(st, [mkbar("TST", m, 10, 10, 10, 10) for m in CAL.regular_minutes(TODAY)[:40]])
    snap = compute_all(st, as_of)
    assert set(snap) == set(FEATURES)


# ---------------------------------------------------------------------------- filters
@pytest.mark.parametrize(
    ("op", "val", "expected"),
    [
        ("gt", 10, False),
        ("gte", 10, True),
        ("lt", 11, True),
        ("lte", 9, False),
        ("eq", 10, True),
        ("neq", 10, False),
        ("between", [9, 11], True),
        ("outside", [9, 11], False),
    ],
)
def test_numeric_operators(op, val, expected):
    st = state_with_history(price=10.0)
    as_of = feed(st, [mkbar("TST", CAL.regular_minutes(TODAY)[0], 10, 10, 10, 10)])
    assert evaluate_filter(F(field="last", operator=op, value=val), EvalContext(st, as_of)).passed is expected


def test_boolean_filter_operators():
    st = state_with_history()
    as_of = feed(st, [mkbar("TST", CAL.regular_minutes(TODAY)[0], 10, 10, 10, 10)])
    assert evaluate_filter(F(field="halted", operator="is_false", value=None), EvalContext(st, as_of)).passed
    st.halted = True
    assert evaluate_filter(F(field="halted", operator="is_true", value=None), EvalContext(st, as_of)).passed


def test_null_policies():
    st = SymbolState("TST", CAL)  # no data at all
    as_of = CAL.open_dt(TODAY) + timedelta(minutes=5)
    e = EvalContext(st, as_of)
    r = evaluate_filter(F(field="rvol", operator="gt", value=1, null_policy="fail"), e)
    assert r.is_null and not r.passed
    r = evaluate_filter(F(field="rvol", operator="gt", value=1, null_policy="pass"), e)
    assert r.is_null and r.passed and not r.skipped
    r = evaluate_filter(F(field="rvol", operator="gt", value=1, null_policy="skip"), e)
    assert r.is_null and r.passed and r.skipped


def test_formula_filter_with_div_by_zero_is_null():
    st = state_with_history(price=10.0)
    as_of = feed(st, [mkbar("TST", CAL.regular_minutes(TODAY)[0], 10, 10, 10, 10)])
    f = F(field="formula", operator="is_true", value=None, formula="range / range > 1", null_policy="fail")
    r = evaluate_filter(f, EvalContext(st, as_of))
    assert r.is_null and not r.passed and "division_by_zero" in r.notes


def test_filter_validation_errors():
    with pytest.raises(ValidationError, match="unit mismatch"):
        F(field="last", operator="gt", value=1, unit="pct")
    with pytest.raises(ValidationError, match="unknown field"):
        F(field="nonsense", operator="gt", value=1)
    with pytest.raises(ValidationError, match="lookback"):
        F(field="last", operator="gt", value=1, lookback=5)
    with pytest.raises(ValidationError, match="lookback"):
        F(field="sma", operator="gt", value=1, lookback=100000)
    with pytest.raises(ValidationError, match="between"):
        F(field="last", operator="between", value=[5, 1])
    with pytest.raises(ValidationError, match="numeric"):
        F(field="last", operator="gt", value="abc")
    with pytest.raises(ValidationError, match="boolean field"):
        F(field="halted", operator="gt", value=1)
    with pytest.raises(ValidationError, match="formula error"):
        F(field="formula", operator="is_true", formula="last +")
    with pytest.raises(ValidationError, match="requires the 'formula'"):
        F(field="formula", operator="is_true")


def test_and_semantics_and_disabled_filters():
    st = state_with_history(price=10.0)
    as_of = feed(st, [mkbar("TST", CAL.regular_minutes(TODAY)[0], 10, 10, 10, 10)])
    pass_f = F(id="a", field="last", operator="gt", value=5)
    fail_f = F(id="b", field="last", operator="gt", value=50)
    ok, res = evaluate_all([pass_f, fail_f], EvalContext(st, as_of))
    assert not ok and [r.passed for r in res] == [True, False]
    ok, res = evaluate_all([pass_f, fail_f.model_copy(update={"enabled": False})], EvalContext(st, as_of))
    assert ok and len(res) == 1
