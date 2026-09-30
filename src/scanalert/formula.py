"""Constrained, versioned formula language.

Design goals: user-supplied expressions can never execute code. The text is tokenised and parsed by a
hand-written Pratt parser into a small immutable AST, type/unit-checked, and evaluated by a tree walker
that only knows numbers, booleans, null and a fixed function table. No ``eval``/``exec``/``compile``.

Grammar (lowest to highest precedence)::

    expr     := or
    or       := and (('or' | '||') and)*
    and      := not (('and' | '&&') not)*
    not      := ('not' | '!') not | compare
    compare  := add (('<'|'<='|'>'|'>='|'=='|'!=') add)?
    add      := mul (('+'|'-') mul)*
    mul      := unary (('*'|'/') unary)*
    unary    := '-' unary | primary
    primary  := NUMBER | 'true' | 'false' | 'null' | IDENT | IDENT '(' args ')' | '(' expr ')'

* ``IDENT`` names a feature from :data:`scanalert.features.FEATURES` (``rvol``, ``vwap_dist_pct`` ...).
* ``feature(n)`` supplies a bounded integer lookback: ``sma(20)``, ``volatility(30)``, ``orb_high(15)``.
* Functions: ``abs min max coalesce isnull if``. Conditional: ``if(cond, then, else)``.

Null policy: arithmetic/comparison with null yields null; ``and``/``or`` use Kleene logic; division by
zero yields null and records a note. The *filter* decides what a final null means (``null_policy``).
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Protocol

from .features import FEATURES, MAX_LOOKBACK

MAX_EXPR_LEN = 500
MAX_NODES = 120
MAX_DEPTH = 24
MAX_ABS = 1e12

FUNCTIONS = {"abs": 1, "min": 2, "max": 2, "coalesce": 2, "isnull": 1, "if": 3}
KEYWORDS = {"and", "or", "not", "true", "false", "null"}


class FormulaError(ValueError):
    def __init__(self, message: str, position: int | None = None):
        self.message = message
        self.position = position
        super().__init__(message if position is None else f"{message} (at column {position + 1})")

    def as_dict(self) -> dict[str, object]:
        return {"message": self.message, "position": self.position}


# ------------------------------------------------------------------------------------------- AST
@dataclass(frozen=True)
class Num:
    value: float


@dataclass(frozen=True)
class Bool:
    value: bool


@dataclass(frozen=True)
class Null:
    pass


@dataclass(frozen=True)
class Feat:
    name: str
    lookback: int | None = None


@dataclass(frozen=True)
class Unary:
    op: str  # '-' | 'not'
    operand: Node


@dataclass(frozen=True)
class Binary:
    op: str
    left: Node
    right: Node


@dataclass(frozen=True)
class Call:
    fn: str
    args: tuple[Node, ...]


Node = Num | Bool | Null | Feat | Unary | Binary | Call


# ------------------------------------------------------------------------------------------ lexer
_TOKEN = re.compile(
    r"""\s*(?:
    (?P<num>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|\.\d+)
   |(?P<id>[A-Za-z_][A-Za-z0-9_]*)
   |(?P<op><=|>=|==|!=|&&|\|\||[-+*/<>!(),])
    )""",
    re.X,
)


@dataclass(frozen=True)
class Tok:
    kind: str  # num id op end
    text: str
    pos: int


def tokenize(src: str) -> list[Tok]:
    if len(src) > MAX_EXPR_LEN:
        raise FormulaError(f"expression too long ({len(src)} > {MAX_EXPR_LEN} characters)")
    toks: list[Tok] = []
    i = 0
    while i < len(src):
        if src[i].isspace():
            i += 1
            continue
        m = _TOKEN.match(src, i)
        if not m or m.end() == i:
            raise FormulaError(f"unexpected character {src[i]!r}", i)
        kind = m.lastgroup or ""
        text = m.group(kind)
        toks.append(Tok(kind, text, m.start(kind)))
        i = m.end()
    toks.append(Tok("end", "", len(src)))
    return toks


# ----------------------------------------------------------------------------------------- parser
class _Parser:
    def __init__(self, src: str):
        self.toks = tokenize(src)
        self.i = 0
        self.nodes = 0

    def peek(self) -> Tok:
        return self.toks[self.i]

    def next(self) -> Tok:
        t = self.toks[self.i]
        self.i += 1
        return t

    def _count(self) -> None:
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise FormulaError(f"expression too complex (more than {MAX_NODES} nodes)")

    def _is(self, t: Tok, *texts: str) -> bool:
        if t.kind == "id":
            return t.text.lower() in texts
        return t.kind == "op" and t.text in texts

    def parse(self) -> Node:
        node = self.or_(0)
        if self.peek().kind != "end":
            raise FormulaError(f"unexpected token {self.peek().text!r}", self.peek().pos)
        return node

    def or_(self, depth: int) -> Node:
        left = self.and_(depth)
        while self._is(self.peek(), "or", "||"):
            self.next()
            self._count()
            left = Binary("or", left, self.and_(depth))
        return left

    def and_(self, depth: int) -> Node:
        left = self.not_(depth)
        while self._is(self.peek(), "and", "&&"):
            self.next()
            self._count()
            left = Binary("and", left, self.not_(depth))
        return left

    def not_(self, depth: int) -> Node:
        if depth > MAX_DEPTH:
            raise FormulaError(f"expression nested too deeply (> {MAX_DEPTH})", self.peek().pos)
        if self._is(self.peek(), "not", "!"):
            self.next()
            self._count()
            return Unary("not", self.not_(depth + 1))
        return self.compare(depth)

    def compare(self, depth: int) -> Node:
        left = self.add(depth)
        t = self.peek()
        if t.kind == "op" and t.text in ("<", "<=", ">", ">=", "==", "!="):
            self.next()
            self._count()
            right = self.add(depth)
            n = self.peek()
            if n.kind == "op" and n.text in ("<", "<=", ">", ">=", "==", "!="):
                raise FormulaError("chained comparisons are not supported; use 'and'", n.pos)
            return Binary(t.text, left, right)
        return left

    def add(self, depth: int) -> Node:
        left = self.mul(depth)
        while self.peek().kind == "op" and self.peek().text in ("+", "-"):
            op = self.next().text
            self._count()
            left = Binary(op, left, self.mul(depth))
        return left

    def mul(self, depth: int) -> Node:
        left = self.unary(depth)
        while self.peek().kind == "op" and self.peek().text in ("*", "/"):
            op = self.next().text
            self._count()
            left = Binary(op, left, self.unary(depth))
        return left

    def unary(self, depth: int) -> Node:
        if depth > MAX_DEPTH:
            raise FormulaError(f"expression nested too deeply (> {MAX_DEPTH})", self.peek().pos)
        t = self.peek()
        if t.kind == "op" and t.text == "-":
            self.next()
            self._count()
            return Unary("-", self.unary(depth + 1))
        return self.primary(depth)

    def primary(self, depth: int) -> Node:
        t = self.next()
        self._count()
        if t.kind == "num":
            v = float(t.text)
            if not math.isfinite(v) or abs(v) > MAX_ABS:
                raise FormulaError("numeric literal out of range", t.pos)
            return Num(v)
        if t.kind == "op" and t.text == "(":
            if depth + 1 > MAX_DEPTH:
                raise FormulaError(f"expression nested too deeply (> {MAX_DEPTH})", t.pos)
            inner = self.or_(depth + 1)
            close = self.next()
            if not (close.kind == "op" and close.text == ")"):
                raise FormulaError("expected ')'", close.pos)
            return inner
        if t.kind == "id":
            word = t.text.lower()
            if word == "true":
                return Bool(True)
            if word == "false":
                return Bool(False)
            if word == "null":
                return Null()
            if word in KEYWORDS:
                raise FormulaError(f"unexpected keyword {t.text!r}", t.pos)
            if self.peek().kind == "op" and self.peek().text == "(":
                return self.call(t, depth)
            if t.text not in FEATURES and t.text not in FUNCTIONS:
                raise FormulaError(f"unknown identifier {t.text!r}{_suggest(t.text)}", t.pos)
            if t.text in FUNCTIONS:
                raise FormulaError(f"function {t.text!r} must be called with parentheses", t.pos)
            return Feat(t.text, None)
        if t.kind == "end":
            raise FormulaError("unexpected end of expression", t.pos)
        raise FormulaError(f"unexpected token {t.text!r}", t.pos)

    def call(self, name: Tok, depth: int) -> Node:
        self.next()  # (
        args: list[Node] = []
        if not (self.peek().kind == "op" and self.peek().text == ")"):
            while True:
                args.append(self.or_(depth + 1))
                if self.peek().kind == "op" and self.peek().text == ",":
                    self.next()
                    continue
                break
        close = self.next()
        if not (close.kind == "op" and close.text == ")"):
            raise FormulaError("expected ')' or ','", close.pos)
        fname = name.text
        if fname in FUNCTIONS:
            if len(args) != FUNCTIONS[fname]:
                raise FormulaError(
                    f"{fname}() takes {FUNCTIONS[fname]} argument(s), got {len(args)}", name.pos
                )
            return Call(fname, tuple(args))
        fd = FEATURES.get(fname)
        if fd is None:
            raise FormulaError(f"unknown function or feature {fname!r}{_suggest(fname)}", name.pos)
        if fd.default_lookback is None:
            raise FormulaError(f"feature {fname!r} does not take a lookback", name.pos)
        if len(args) != 1 or not isinstance(args[0], Num) or args[0].value != int(args[0].value):
            raise FormulaError(f"{fname}(n) requires one integer literal lookback", name.pos)
        n = int(args[0].value)
        if not 1 <= n <= min(fd.max_lookback, MAX_LOOKBACK):
            raise FormulaError(f"lookback for {fname} must be between 1 and {fd.max_lookback}", name.pos)
        return Feat(fname, n)


def _suggest(name: str) -> str:
    cands = [k for k in list(FEATURES) + list(FUNCTIONS) if k.startswith(name[:3])]
    return f"; did you mean {', '.join(sorted(cands)[:4])}?" if cands else ""


def parse(src: str) -> Node:
    if not isinstance(src, str) or not src.strip():
        raise FormulaError("expression is empty")
    return _Parser(src).parse()


# --------------------------------------------------------------------------------- type checking
@dataclass(frozen=True)
class Ty:
    type: str  # number | bool | null
    unit: str = "scalar"  # scalar (unitless literal) or a feature unit / derived:...


def _compat(a: str, b: str) -> bool:
    return a == b or "scalar" in (a, b)


def _unit_of(a: Ty, b: Ty) -> str:
    return b.unit if a.unit == "scalar" else a.unit


def check(node: Node) -> Ty:
    """Static type + unit validation; raises FormulaError. Returns the expression type."""
    if isinstance(node, Num):
        return Ty("number")
    if isinstance(node, Bool):
        return Ty("bool", "bool")
    if isinstance(node, Null):
        return Ty("null")
    if isinstance(node, Feat):
        fd = FEATURES[node.name]
        return Ty(fd.type, fd.unit)
    if isinstance(node, Unary):
        t = check(node.operand)
        if node.op == "not":
            if t.type not in ("bool", "null"):
                raise FormulaError("'not' requires a boolean operand")
            return Ty("bool", "bool")
        if t.type not in ("number", "null"):
            raise FormulaError("unary '-' requires a numeric operand")
        return t
    if isinstance(node, Binary):
        lt, rt = check(node.left), check(node.right)
        op = node.op
        if op in ("and", "or"):
            for t in (lt, rt):
                if t.type not in ("bool", "null"):
                    raise FormulaError(f"'{op}' requires boolean operands, got {t.type}")
            return Ty("bool", "bool")
        if op in ("+", "-"):
            _need_numeric(op, lt, rt)
            if not _compat(lt.unit, rt.unit):
                raise FormulaError(
                    f"unit mismatch: cannot {'add' if op == '+' else 'subtract'} {lt.unit} and {rt.unit}"
                )
            return Ty("number", _unit_of(lt, rt))
        if op in ("*", "/"):
            _need_numeric(op, lt, rt)
            lu, ru = lt.unit, rt.unit
            if lu in ("scalar", "ratio") and ru in ("scalar", "ratio"):
                return Ty("number", "ratio" if "ratio" in (lu, ru) else "scalar")
            if op == "*":
                if ru in ("scalar", "ratio"):
                    return Ty("number", lu)
                if lu in ("scalar", "ratio"):
                    return Ty("number", ru)
                return Ty("number", f"derived:{lu}*{ru}")
            if lu == ru:
                return Ty("number", "ratio")
            if ru in ("scalar", "ratio"):
                return Ty("number", lu)
            return Ty("number", f"derived:{lu}/{ru}")
        # comparisons
        if op in ("==", "!="):
            if lt.type != rt.type and "null" not in (lt.type, rt.type):
                raise FormulaError(f"cannot compare {lt.type} with {rt.type}")
        else:
            _need_numeric(op, lt, rt)
        if lt.type != "bool" and rt.type != "bool" and not _compat(lt.unit, rt.unit):
            raise FormulaError(f"unit mismatch: cannot compare {lt.unit} with {rt.unit}")
        return Ty("bool", "bool")
    if isinstance(node, Call):
        ts = [check(a) for a in node.args]
        fn = node.fn
        if fn == "isnull":
            return Ty("bool", "bool")
        if fn == "abs":
            if ts[0].type not in ("number", "null"):
                raise FormulaError("abs() requires a number")
            return ts[0]
        if fn in ("min", "max", "coalesce"):
            if fn != "coalesce":
                for t in ts:
                    if t.type not in ("number", "null"):
                        raise FormulaError(f"{fn}() requires numbers")
            a, b = ts
            if a.type not in (b.type, "null") and b.type != "null":
                raise FormulaError(f"{fn}() arguments must have the same type")
            if not _compat(a.unit, b.unit) and "null" not in (a.type, b.type):
                raise FormulaError(f"unit mismatch in {fn}(): {a.unit} vs {b.unit}")
            base = b if a.type == "null" else a
            return Ty(base.type, _unit_of(a, b))
        if fn == "if":
            c, a, b = ts
            if c.type not in ("bool", "null"):
                raise FormulaError("if() condition must be boolean")
            if a.type != b.type and "null" not in (a.type, b.type):
                raise FormulaError("if() branches must have the same type")
            if not _compat(a.unit, b.unit) and "null" not in (a.type, b.type):
                raise FormulaError(f"unit mismatch between if() branches: {a.unit} vs {b.unit}")
            base = b if a.type == "null" else a
            return Ty(base.type, _unit_of(a, b))
    raise FormulaError("unsupported expression")


def _need_numeric(op: str, a: Ty, b: Ty) -> None:
    for t in (a, b):
        if t.type not in ("number", "null"):
            raise FormulaError(f"operator '{op}' requires numeric operands, got {t.type}")


# ------------------------------------------------------------------------------------ evaluation
class Resolver(Protocol):
    notes: list[str]

    def get(self, name: str, lookback: int | None = None) -> float | bool | None: ...


Value = float | bool | None


def evaluate(node: Node, r: Resolver) -> Value:
    if isinstance(node, Num):
        return node.value
    if isinstance(node, Bool):
        return node.value
    if isinstance(node, Null):
        return None
    if isinstance(node, Feat):
        return r.get(node.name, node.lookback)
    if isinstance(node, Unary):
        v = evaluate(node.operand, r)
        if v is None:
            return None
        return (not v) if node.op == "not" else -float(v)
    if isinstance(node, Binary):
        op = node.op
        if op in ("and", "or"):
            a = evaluate(node.left, r)
            if op == "and":
                if a is False:
                    return False
                b = evaluate(node.right, r)
                if b is False:
                    return False
                return None if a is None or b is None else True
            if a is True:
                return True
            b = evaluate(node.right, r)
            if b is True:
                return True
            return None if a is None or b is None else False
        a, b = evaluate(node.left, r), evaluate(node.right, r)
        if a is None or b is None:
            return None
        if op in ("==", "!="):
            eq = a == b
            return eq if op == "==" else not eq
        x, y = float(a), float(b)
        if op == "+":
            return x + y
        if op == "-":
            return x - y
        if op == "*":
            return x * y
        if op == "/":
            if y == 0:
                r.notes.append("division_by_zero")
                return None
            return x / y
        if op == "<":
            return x < y
        if op == "<=":
            return x <= y
        if op == ">":
            return x > y
        if op == ">=":
            return x >= y
    if isinstance(node, Call):
        fn = node.fn
        if fn == "if":
            c = evaluate(node.args[0], r)
            if c is None:
                return None
            return evaluate(node.args[1] if c else node.args[2], r)
        vals = [evaluate(a, r) for a in node.args]
        if fn == "isnull":
            return vals[0] is None
        if fn == "coalesce":
            return vals[0] if vals[0] is not None else vals[1]
        if any(v is None for v in vals):
            return None
        if fn == "abs":
            return abs(float(vals[0]))  # type: ignore[arg-type]
        if fn == "min":
            return min(float(vals[0]), float(vals[1]))  # type: ignore[arg-type]
        if fn == "max":
            return max(float(vals[0]), float(vals[1]))  # type: ignore[arg-type]
    raise FormulaError("unsupported expression")


# --------------------------------------------------------------------------------- canonical form
def to_source(node: Node) -> str:
    """Deterministic, fully parenthesised rendering used for hashing and display."""
    if isinstance(node, Num):
        return repr(int(node.value)) if node.value == int(node.value) else repr(node.value)
    if isinstance(node, Bool):
        return "true" if node.value else "false"
    if isinstance(node, Null):
        return "null"
    if isinstance(node, Feat):
        return node.name if node.lookback is None else f"{node.name}({node.lookback})"
    if isinstance(node, Unary):
        return f"(not {to_source(node.operand)})" if node.op == "not" else f"(-{to_source(node.operand)})"
    if isinstance(node, Binary):
        return f"({to_source(node.left)} {node.op} {to_source(node.right)})"
    if isinstance(node, Call):
        return f"{node.fn}({', '.join(to_source(a) for a in node.args)})"
    raise FormulaError("unsupported expression")


def features_used(node: Node) -> set[tuple[str, int | None]]:
    if isinstance(node, Feat):
        return {(node.name, node.lookback)}
    if isinstance(node, Unary):
        return features_used(node.operand)
    if isinstance(node, Binary):
        return features_used(node.left) | features_used(node.right)
    if isinstance(node, Call):
        out: set[tuple[str, int | None]] = set()
        for a in node.args:
            out |= features_used(a)
        return out
    return set()


@dataclass(frozen=True)
class CompiledFormula:
    source: str
    ast: Node
    type: str
    unit: str
    canonical: str
    digest: str
    features: tuple[tuple[str, int | None], ...]

    def evaluate(self, r: Resolver) -> Value:
        return evaluate(self.ast, r)


def compile_formula(src: str, expect: str | None = None) -> CompiledFormula:
    """Parse + type-check. ``expect='bool'`` requires a boolean-valued expression."""
    ast = parse(src)
    ty = check(ast)
    if expect and ty.type not in (expect, "null"):
        raise FormulaError(f"formula must evaluate to {expect}, not {ty.type}")
    canon = to_source(ast)
    return CompiledFormula(
        source=src,
        ast=ast,
        type=ty.type,
        unit=ty.unit,
        canonical=canon,
        digest=hashlib.sha256(canon.encode()).hexdigest()[:16],
        features=tuple(sorted(features_used(ast), key=lambda x: (x[0], x[1] or 0))),
    )
