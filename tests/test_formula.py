from __future__ import annotations

import pytest

from scanalert.formula import FormulaError, compile_formula


class R:
    """Resolver stub."""

    def __init__(self, **vals):
        self.vals, self.notes = vals, []

    def get(self, name, lookback=None):
        return self.vals.get((name, lookback), self.vals.get(name))


def ev(src, **vals):
    return compile_formula(src).evaluate(R(**vals))


def test_arithmetic_and_precedence():
    assert ev("1 + 2 * 3") == 7
    assert ev("(1 + 2) * 3") == 9
    assert ev("-2 + 5") == 3
    assert ev("10 / 4") == 2.5


def test_comparisons_and_boolean_logic():
    assert ev("rvol > 2 and pct_change < 5", rvol=3.0, pct_change=1.0) is True
    assert ev("rvol > 2 and pct_change < 5", rvol=3.0, pct_change=9.0) is False
    assert ev("rvol > 2 or pct_change < 5", rvol=1.0, pct_change=9.0) is False
    assert ev("not halted", halted=False) is True
    assert ev("rvol >= 2 && last <= 10 || halted", rvol=2.0, last=11.0, halted=True) is True


def test_conditional_expression():
    assert ev("if(rvol > 2, 1, 0) == 1", rvol=3.0) is True
    assert ev("if(rvol > 2, last * 2, last)", rvol=1.0, last=5.0) == 5.0


def test_functions():
    assert ev("abs(-3.5)") == 3.5
    assert ev("min(2, 3)") == 2
    assert ev("max(2, 3)") == 3
    assert ev("coalesce(rvol, 7)", rvol=None) == 7
    assert ev("isnull(rvol)", rvol=None) is True


def test_lookback_syntax_bounded():
    assert compile_formula("sma(20) < last").features == (("last", None), ("sma", 20))
    with pytest.raises(FormulaError, match="between 1 and"):
        compile_formula("sma(9999) < last")
    with pytest.raises(FormulaError, match="integer literal"):
        compile_formula("sma(last) < last")
    with pytest.raises(FormulaError, match="does not take a lookback"):
        compile_formula("last(5) > 1")


def test_null_propagation_and_kleene_logic():
    assert ev("rvol > 2", rvol=None) is None
    assert ev("rvol + 1", rvol=None) is None
    assert ev("rvol > 2 and halted", rvol=None, halted=False) is False  # False dominates
    assert ev("rvol > 2 or halted", rvol=None, halted=True) is True  # True dominates
    assert ev("rvol > 2 and halted", rvol=None, halted=True) is None
    assert ev("not (rvol > 2)", rvol=None) is None
    assert ev("if(rvol > 2, 1, 0)", rvol=None) is None


def test_divide_by_zero_is_null_with_note():
    r = R()
    assert compile_formula("1 / 0").evaluate(r) is None
    assert "division_by_zero" in r.notes
    assert ev("last / (last - last) > 1", last=5.0) is None


def test_type_errors():
    with pytest.raises(FormulaError, match="boolean"):
        compile_formula("last and halted")
    with pytest.raises(FormulaError, match="numeric"):
        compile_formula("halted + 1")
    with pytest.raises(FormulaError, match="must evaluate to bool"):
        compile_formula("last + 1", expect="bool")
    with pytest.raises(FormulaError, match="condition must be boolean"):
        compile_formula("if(last, 1, 2)")


def test_unit_validation():
    with pytest.raises(FormulaError, match="unit mismatch"):
        compile_formula("last + pct_change")
    with pytest.raises(FormulaError, match="unit mismatch"):
        compile_formula("last > rvol")
    compile_formula("last / vwap > 1")  # usd/usd -> ratio vs literal ok
    compile_formula("pct_change + 1 > gap_pct")
    compile_formula("rvol * 2 > 3")


@pytest.mark.parametrize(
    "src",
    [
        "__import__('os').system('id')",
        "last.__class__",
        "eval('1')",
        "open('/etc/passwd')",
        "lambda: 1",
        "last; drop table alerts",
        "`whoami`",
        "$(whoami)",
        "1 if last else 2",
        "[1,2,3]",
        "{'a':1}",
        "last = 5",
        "exec(code)",
    ],
)
def test_no_code_execution_only_validation_errors(src):
    with pytest.raises(FormulaError):
        compile_formula(src)


def test_errors_report_position():
    with pytest.raises(FormulaError) as ei:
        compile_formula("rvol > 2 and foo > 1")
    assert ei.value.position == 13 and "foo" in ei.value.message
    with pytest.raises(FormulaError, match="unexpected end"):
        compile_formula("rvol >")
    with pytest.raises(FormulaError, match="chained"):
        compile_formula("1 < rvol < 3")


def test_limits():
    with pytest.raises(FormulaError, match="too long"):
        compile_formula("1+" * 400 + "1")
    with pytest.raises(FormulaError, match="too deeply|too complex"):
        compile_formula("(" * 40 + "1" + ")" * 40)
    with pytest.raises(FormulaError, match="too complex"):
        compile_formula(" + ".join(["1"] * 100) + " > 1")
    with pytest.raises(FormulaError):
        compile_formula("")
    with pytest.raises(FormulaError, match="out of range"):
        compile_formula("1e300 > 1")


def test_canonical_form_and_digest_stable():
    a = compile_formula("rvol>2   and   vwap_dist_pct>0.5")
    b = compile_formula("(rvol > 2) and (vwap_dist_pct > 0.5)")
    assert a.canonical == b.canonical and a.digest == b.digest
    assert a.digest != compile_formula("rvol > 3 and vwap_dist_pct > 0.5").digest
