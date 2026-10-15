"""Linear state-carrying Z3 encoding of past-time policies over one trace (PLAN.md, design decision 2)."""
import re
from functools import lru_cache
from typing import Any, Callable, Hashable, Sequence

import z3

from app.policy import ast
from app.policy.events import Event

_SMT_CMP = {"<": "<", "<=": "<=", "==": "=", "!=": None, ">=": ">=", ">": ">"}
_INLINE_MAX = 40


# matching is deliberately not shared with reference.py: the reference evaluator is an independent oracle
def _part(part: str) -> str | re.Pattern:
    return re.compile("".join("." if c == "?" else re.escape(c) for c in part), re.S) if "?" in part else part


def compile_glob(glob: str) -> Callable[[str], bool]:
    """Star-free parts are fixed-length; the middle ones are found leftmost-first, so nothing re-splits (no
    backtracking; at worst len(name) x len(glob)). Empty middle parts (from `**`) match anywhere and are dropped."""
    first, *rest = glob.split("*")
    if not rest:
        if "?" not in first:
            return first.__eq__
        whole = _part(first)
        return lambda name: whole.fullmatch(name) is not None
    head, n0 = _part(first), len(first)
    tail, n1 = _part(rest[-1]), len(rest[-1])
    mid = [(_part(p), len(p)) for p in rest[:-1] if p]

    def match(name: str) -> bool:
        if not (name.startswith(head) if isinstance(head, str) else head.match(name)):
            return False
        pos = n0
        for p, n in mid:
            if isinstance(p, str):
                i = name.find(p, pos)
                if i < 0:
                    return False
                pos = i + n
            else:
                m = p.search(name, pos)
                if m is None:
                    return False
                pos = m.end()
        end = len(name) - n1
        return end >= pos and (name.endswith(tail) if isinstance(tail, str) else tail.match(name, end) is not None)
    return match


def glob_cost(glob: str) -> int:
    """Work units (`checker.work`) of matching one name: one, two per `*` between parts, and one per character of
    a part with `?` (a regex that may try each of its characters at each position of the name)."""
    parts = [p for p in glob.split("*") if p]
    return 1 + 2 * max(len(parts) - 1, 0) + sum(len(p) for p in parts if "?" in p)


_glob_match = lru_cache(maxsize=4096)(compile_glob)


def glob_match(glob: str, name: str) -> bool:
    return _glob_match(glob)(name)


def attr_equal(actual: Any, expected: Any) -> bool:
    return type(actual) is type(expected) and actual == expected


def matches(m: ast.Match, e: Event) -> bool:
    return ((m.kind is None or e.kind == m.kind)
            and (not m.globs or any(glob_match(g, e.name) for g in m.globs))
            and (m.status is None or e.status == m.status)
            and all(k in e.attrs and attr_equal(e.attrs[k], v) for k, v in m.attrs))


def compile_match(m: ast.Match) -> Callable[[Event], bool]:
    """`matches(m, .)` with its globs compiled once, for a plan that outlives any shared glob cache."""
    globs = tuple(map(compile_glob, dict.fromkeys(m.globs)))
    return lambda e: ((m.kind is None or e.kind == m.kind)
                      and (not globs or any(g(e.name) for g in globs))
                      and (m.status is None or e.status == m.status)
                      and all(k in e.attrs and attr_equal(e.attrs[k], v) for k, v in m.attrs))


def _match_key(m: ast.Match) -> Hashable:
    # dataclass equality treats 1 == True; attr values must stay type-distinct
    return ("M", m.kind, m.globs, m.status, tuple((k, type(v).__name__, v) for k, v in m.attrs))


class Dag:
    """Distinct subformulas, children first. Tags make a formula a DAG whose tree can be exponentially larger,
    so each node object is visited once and equal subformulas get one index."""

    def __init__(self):
        self.nodes: list[ast.Formula] = []
        self.kids: list[tuple[int, ...]] = []
        self.keys: dict[Hashable, int] = {}
        self._seen: dict[int, tuple[ast.Formula, int]] = {}

    def add(self, f: ast.Formula) -> int:
        hit = self._seen.get(id(f))
        if hit is not None and hit[0] is f:
            return hit[1]
        if isinstance(f, ast.Match):
            kids, key = (), _match_key(f)
        elif isinstance(f, ast.Count):
            kids = (self.add(f.pred),)
            key = ("Count", *kids, f.op, f.bound, f.by)
        elif isinstance(f, (ast.Not, ast.Once, ast.Historically, ast.Previously)):
            kids = (self.add(f.arg),)
            key = (type(f).__name__, *kids)
        elif isinstance(f, (ast.And, ast.Or, ast.Implies, ast.Since)):
            kids = (self.add(f.left), self.add(f.right))
            key = (type(f).__name__, *kids)
        else:
            raise TypeError(f"not a formula node: {f!r}")
        if key not in self.keys:
            self.keys[key] = len(self.nodes)
            self.nodes.append(f)
            self.kids.append(kids)
        self._seen[id(f)] = (f, self.keys[key])
        return self.keys[key]


class TraceEncoding:
    """Event facts and policy formulas over `events` as SMT-LIB terms, built once and shared across policies.

    `values(f)[i]` is the term for `f` at event i. `facts[i]` are the atom literals observed at event i;
    `defs` define the temporal, count and long Boolean terms. `defs` alone constrain nothing about the
    events: every assignment of the atom variables extends to exactly one model of them.
    """

    def __init__(self, events: Sequence[Event]):
        self.events = list(events)
        self.decls: list[str] = []
        self.defs: list[str] = []
        self.facts: list[list[str]] = [[] for _ in self.events]
        self._dag = Dag()
        self._values: list[list[str]] = []
        self._fresh = 0

    def values(self, f: ast.Formula) -> list[str]:
        root, dag = self._dag.add(f), self._dag
        for k in range(len(self._values), len(dag.nodes)):
            g = dag.nodes[k]
            self._values.append(getattr(self, f"_{type(g).__name__.lower()}")(g, *(self._values[j] for j in dag.kids[k])))
        return self._values[root]

    def smt2(self, *extra: str) -> str:
        asserts = self.defs + [lit for fs in self.facts for lit in fs] + list(extra)
        return "\n".join(self.decls + [f"(assert {a})" for a in asserts])

    def declare(self, sort: str, prefix: str) -> str:
        self._fresh += 1
        name = f"{prefix}{self._fresh}"
        self.decls.append(f"(declare-const {name} {sort})")
        return name

    def define(self, prefix: str, term: str, sort: str = "Bool") -> str:
        name = self.declare(sort, prefix)
        self.defs.append(f"(= {name} {term})")
        return name

    def _short(self, term: str) -> str:
        # shared subterms are repeated in the text; naming long ones keeps the encoding linear
        return term if len(term) <= _INLINE_MAX else self.define("b", term)

    def _match(self, m: ast.Match) -> list[str]:
        if m == ast.Match():
            return ["true"] * len(self.events)
        out = []
        for i, e in enumerate(self.events):
            v = self.declare("Bool", "a")
            self.facts[i].append(v if matches(m, e) else f"(not {v})")
            out.append(v)
        return out

    def _chain(self, prefix: str, first: list[str], step) -> list[str]:
        out: list[str] = []
        for i, x in enumerate(first):
            out.append(self.define(prefix, step(i, out[-1]) if out else x))
        return out

    def _not(self, f, xs):
        return [self._short(f"(not {x})") for x in xs]

    def _and(self, f, xs, ys):
        return [self._short(f"(and {x} {y})") for x, y in zip(xs, ys)]

    def _or(self, f, xs, ys):
        return [self._short(f"(or {x} {y})") for x, y in zip(xs, ys)]

    def _implies(self, f, xs, ys):
        return [self._short(f"(=> {x} {y})") for x, y in zip(xs, ys)]

    def _previously(self, f, xs):
        return ["false"] + xs[:-1] if xs else []

    def _once(self, f, xs):
        return self._chain("o", xs, lambda i, prev: f"(or {xs[i]} {prev})")

    def _historically(self, f, xs):
        return self._chain("h", xs, lambda i, prev: f"(and {xs[i]} {prev})")

    def _since(self, f, xs, ys):
        return self._chain("s", ys, lambda i, prev: f"(or {ys[i]} (and {xs[i]} {prev}))")

    def _count(self, f: ast.Count, xs):
        last: dict[Any, str] = {}
        op = _SMT_CMP[f.op]
        out = []
        for e, x in zip(self.events, xs):
            group = None if f.by is None else getattr(e, f.by)
            c = self.define("c", f"(+ {last.get(group, 0)} (ite {x} 1 0))", "Int")
            last[group] = c
            out.append(f"(not (= {c} {f.bound}))" if op is None else f"({op} {c} {f.bound})")
        return out


def first_violations(formulas: Sequence[ast.Formula], events: Sequence[Event]) -> list[int | None]:
    """For each formula, the first seq at which it is false on `events`, or None."""
    n = len(events)
    if not n or not formulas:
        return [None] * len(formulas)
    enc = TraceEncoding(events)
    firsts = []
    for f in formulas:
        # index of the first false position, n if none
        ite = "".join(f"(ite (not {v}) {i} " for i, v in enumerate(enc.values(f))) + str(n) + ")" * n
        firsts.append(enc.define("f", ite, "Int"))
    ctx = z3.Context()  # the global context is not thread-safe
    solver = z3.Then("simplify", "propagate-values", "solve-eqs", "smt", ctx=ctx).solver()
    solver.from_string(enc.smt2())
    if solver.check() != z3.sat:
        raise RuntimeError("trace encoding is unsatisfiable")
    model = solver.model()
    out = []
    for name in firsts:
        i = model.eval(z3.Int(name, ctx), model_completion=True).as_long()
        out.append(events[i].seq if i < n else None)
    return out
