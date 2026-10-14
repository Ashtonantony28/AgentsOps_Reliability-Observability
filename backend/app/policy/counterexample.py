"""Minimal counterexamples (PLAN.md, design decision 4): the fewest events whose facts alone entail a violation."""
from __future__ import annotations

import logging
import re
import time
from collections import deque
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Hashable, Sequence

import z3

from app.policy import ast
from app.policy.dsl import KINDS
from app.policy.encode import Dag, TraceEncoding, attr_equal, glob_regex

if TYPE_CHECKING:
    from app.policy.checker import CompiledPolicy
    from app.policy.events import Event

log = logging.getLogger(__name__)


def _nodes(f: ast.Formula) -> list[ast.Formula]:
    dag = Dag()
    dag.add(f)
    return dag.nodes


def _typed(v: Any) -> Hashable:
    return type(v).__name__, v


def _width(hi: int) -> int:
    return max(1, hi.bit_length())


def _bv(value: int, width: int) -> str:
    return f"(_ bv{value} {width})"


# every counterexample gets this budget; once it runs out, the events not yet ruled out are kept
BUDGET_S = 2.0
# beyond this many glob automaton states (overlapping globs can reach 2^k) names are strings instead
_MAX_NAME_STATES = 512
_SYMBOL = re.compile(r"[a-z_]+\d+")
# counts of window x (bound + 1) up to this are unary; bit-vector adders above, whose unsat cores are near the window
_UNARY_CELLS = 50_000
_BV_CMP = {"<": "bvult", "<=": "bvule", "==": "=", "!=": "=", ">=": "bvuge", ">": "bvugt"}


class _Strings:
    """SMT-LIB string literals and glob regexes over an injective remapping of `chars` into a small range."""

    def __init__(self, chars):
        self.code = {c: 0x30 + i for i, c in enumerate(sorted(chars))}
        self.char = {v: c for c, v in self.code.items()}

    def lit(self, s: str) -> str:
        return '"' + "".join(f"\\u{{{self.code[c]:x}}}" for c in s) + '"'

    def regex(self, glob: str) -> str:
        parts = ["(re.* re.allchar)" if t == "*" else "re.allchar" if t == "?" else f"(str.to_re {self.lit(t)})"
                 for t in re.findall(r"[*?]|[^*?]+", glob)]
        return parts[0] if len(parts) == 1 else f"(re.++ {' '.join(parts)})"


def _closure(glob: str, positions) -> frozenset[int]:
    out = set(positions)
    for p in sorted(out):
        while p < len(glob) and glob[p] == "*":
            p += 1
            out.add(p)
    return frozenset(out)


def _step(glob: str, positions: frozenset[int], c: str) -> frozenset[int]:
    return _closure(glob, {p + (glob[p] != "*") for p in positions if p < len(glob) and glob[p] in ("*", "?", c)})


@lru_cache(maxsize=256)
def name_patterns(globs: tuple[str, ...]) -> tuple[tuple[frozenset[str], str], ...] | None:
    """Every subset of `globs` that is exactly the set some name matches, each with a shortest such name.

    Breadth-first search over the product of the globs' automata, None past `_MAX_NAME_STATES` states.
    Characters no glob mentions all behave alike, so one of them stands for the rest.
    """
    chars = sorted({c for g in globs for c in g} - {"*", "?"})
    other = next(chr(0xE000 + i) for i in range(len(chars) + 1) if chr(0xE000 + i) not in chars)
    start = tuple(_closure(g, {0}) for g in globs)
    names, todo, out = {start: ""}, deque([start]), {}
    while todo:
        state = todo.popleft()
        out.setdefault(frozenset(g for g, ps in zip(globs, state) if len(g) in ps), names[state])
        for c in chars + [other]:
            nxt = tuple(_step(g, ps, c) for g, ps in zip(globs, state))
            if nxt not in names:
                if len(names) == _MAX_NAME_STATES:
                    return None
                names[nxt] = names[state] + c
                todo.append(nxt)
    return tuple(out.items())


class FactEncoding(TraceEncoding):
    """Atoms as terms over per-event kind, name, status and attr variables instead of observed constants.

    Event i's observed values hold under the assumption literal `assumptions[i]`; without it the event ranges
    over every possible event (any of the 5 kinds, any name, status and attrs), so entailment is exact.
    Statuses and attr values are only compared with policy constants, so each is a code: one per constant,
    plus one for anything else (for attrs, also a missing key). A name is, depending on what the policy asks
    of it, the set of policy globs it matches, an identity code (for `count by name`), or a string
    if both, or if the globs overlap too much to enumerate.
    """

    def __init__(self, events: Sequence[Event], formula: ast.Formula):
        super().__init__(events)
        self._vars: set[str] = set()
        self._lazy: dict[str, Any] = {}
        nodes = list(_nodes(formula))
        ms = [m for m in nodes if isinstance(m, ast.Match)]
        self.statuses = list(dict.fromkeys(m.status for m in ms if m.status is not None))
        self.attrs: dict[str, dict[Hashable, Any]] = {}
        for m in ms:
            for k, v in m.attrs:
                self.attrs.setdefault(k, {}).setdefault(_typed(v), v)
        self.globs = tuple(dict.fromkeys(g for m in ms for g in m.globs))
        by_name = any(isinstance(c, ast.Count) and c.by == "name" for c in nodes)
        patterns = name_patterns(self.globs) if self.globs and not by_name else None
        self.name_mode = ("code" if by_name and not self.globs else "pattern" if patterns
                          else "string" if self.globs else None)
        # every code is a bit-vector, so that apart from string names the problem is solved by bit-blasting
        # kinds outside the 5 only come from stored spans; an unconstrained event may take those too
        self.kinds = list(dict.fromkeys([*KINDS, *(e.kind for e in events), *(m.kind for m in ms if m.kind)]))
        self.widths = {"k": _width(len(self.kinds) - 1), "s": _width(len(self.statuses))}
        self.widths.update({f"v{j}_": _width(len(c)) for j, c in enumerate(self.attrs.values())})
        if self.name_mode == "string":
            self.strings = _Strings({c for e in events for c in e.name} | {c for g in self.globs for c in g})
        elif self.name_mode == "code":
            # codes past the observed names are fresh names, enough for every event to have its own
            self.name_codes = {n: i for i, n in enumerate(dict.fromkeys(e.name for e in events))}
            self.widths["n"] = _width(len(self.name_codes) + len(self.events))
        elif self.name_mode == "pattern":
            self.patterns = [p for p, _ in patterns]
            self.widths["n"] = _width(len(self.patterns) - 1)

        self.assumptions = []
        for i, e in enumerate(self.events):
            self.assumptions.append(self.declare("Bool", "ev_a"))
            observed = [f"(= {self._var('k', i)} {self._code('k', self.kinds.index(e.kind))})"]
            self._in_range(self._var("k", i), len(self.kinds) - 1, i)
            if self.name_mode:
                observed.append(f"(= {self._var('n', i)} {self._observed_name(e.name)})")
            if self.name_mode == "pattern":
                self._in_range(self._var("n", i), len(self.patterns) - 1, i)
            for prefix, consts, actual in self._coded(e):
                code = next((c for c, v in enumerate(consts) if actual(v)), len(consts))
                observed.append(f"(= {self._var(prefix, i)} {self._code(prefix, code)})")
                self._in_range(self._var(prefix, i), len(consts), i)
            self.facts[i].append(f"(=> {self.assumptions[i]} (and {' '.join(observed)}))")

    def cone(self, goal: str, deadline: float) -> list[str]:
        """The definitions `goal` depends on, building lazy ones on the way; the rest only slow the solver down."""
        body: dict[str, str] = {}
        seen, todo, indexed = set(), [goal], 0
        while todo:
            for name in _SYMBOL.findall(todo.pop()):
                if name in self._lazy:
                    if time.monotonic() > deadline:
                        raise TimeoutError("counterexample encoding over budget")
                    self.defs.append(f"(= {name} {self._lazy.pop(name)()})")
                if indexed < len(self.defs):
                    body.update((d[3:d.index(" ", 3)], d) for d in self.defs[indexed:])
                    indexed = len(self.defs)
                if name in body and name not in seen:
                    seen.add(name)
                    todo.append(body[name])
        return [d for d in self.defs if d[3:d.index(" ", 3)] in seen]

    def _in_range(self, var: str, hi: int, i: int) -> None:
        if hi + 1 < 2 ** _width(hi):
            self.facts[i].append(f"(bvule {var} {_bv(hi, _width(hi))})")

    def _code(self, prefix: str, value: int) -> str:
        return _bv(value, self.widths[prefix])

    def _observed_name(self, name: str) -> str | int:
        if self.name_mode == "string":
            return self.strings.lit(name)
        if self.name_mode == "code":
            return self._code("n", self.name_codes[name])
        hit = frozenset(g for g in self.globs if glob_regex(g).fullmatch(name))
        return self._code("n", self.patterns.index(hit))

    def _coded(self, e: Event):
        if self.statuses:
            yield "s", self.statuses, lambda s: e.status == s
        for j, (k, consts) in enumerate(self.attrs.items()):
            yield f"v{j}_", list(consts.values()), lambda v, k=k: k in e.attrs and attr_equal(e.attrs[k], v)

    def _var(self, prefix: str, i: int) -> str:
        name = f"ev_{prefix}{i}"
        if name not in self._vars:
            self._vars.add(name)
            sort = "String" if prefix == "n" and self.name_mode == "string" else f"(_ BitVec {self.widths[prefix]})"
            self.decls.append(f"(declare-const {name} {sort})")
        return name

    def _name_in(self, globs: tuple[str, ...], i: int) -> str:
        n = self._var("n", i)
        if self.name_mode == "string":
            return f"(or {' '.join(f'(str.in_re {n} {self.strings.regex(g)})' for g in globs)})"
        wanted = set(globs)
        hits = [f"(= {n} {self._code('n', c)})" for c, p in enumerate(self.patterns) if p & wanted]
        return "false" if not hits else "true" if len(hits) == len(self.patterns) else f"(or {' '.join(hits)})"

    def _atom(self, m: ast.Match, i: int) -> str:
        terms = []
        if m.kind is not None:
            terms.append(f"(= {self._var('k', i)} {self._code('k', self.kinds.index(m.kind))})")
        if m.globs:
            terms.append(self._name_in(m.globs, i))
        if m.status is not None:
            terms.append(f"(= {self._var('s', i)} {self._code('s', self.statuses.index(m.status))})")
        keys = list(self.attrs)
        for k, v in m.attrs:
            prefix = f"v{keys.index(k)}_"
            terms.append(f"(= {self._var(prefix, i)} {self._code(prefix, list(self.attrs[k]).index(_typed(v)))})")
        return terms[0] if len(terms) == 1 else f"(and {' '.join(terms)})"

    def _match(self, m: ast.Match) -> list[str]:
        if m == ast.Match():
            return ["true"] * len(self.events)
        return [self._short(self._atom(m, i)) for i in range(len(self.events))]

    def _count(self, f: ast.Count, xs: list[str]) -> list[str]:
        if f.bound < 0:  # only ASTs built in code; the DSL has no negative bounds
            return ["true" if f.op in (">", ">=", "!=") else "false"] * len(xs)
        # a count is only compared with its bound, so it may saturate one past it; it is kept in unary, as
        # "at least k" literals, which unit propagation decides without bit-blasted adders (see `_UNARY_CELLS`)
        bound = min(f.bound, len(xs) + 1)
        if f.by is not None:
            # quadratic in the window, so a position's term is only built if the goal depends on it (see `cone`)
            out = [self.declare("Bool", "q") for _ in xs]
            for i, q in enumerate(out):
                self._lazy[q] = lambda i=i: self._group_count(f, xs, i, bound)
            return out
        out, c = [], None
        for x in xs:
            c = self._add(c, x, bound, len(xs))
            out.append(self._short(self._compare(f.op, bound, c, len(xs))))
        return out

    def _group_count(self, f: ast.Count, xs: list[str], i: int, bound: int) -> str:
        # the group of event i depends on its facts, so every earlier event is compared with it
        v = "n" if f.by == "name" else "k"
        c = None
        for j in range(i):
            if xs[j] != "false":
                same = f"(= {self._var(v, j)} {self._var(v, i)})"
                c = self._add(c, same if xs[j] == "true" else self.define("b", f"(and {xs[j]} {same})"), bound, i + 1)
        return self._compare(f.op, bound, self._add(c, xs[i], bound, i + 1), i + 1)

    def _add(self, c: list[str] | str | None, t: str, bound: int, n: int) -> list[str] | str:
        """The count `c` (None for zero) after one more event, counted when `t` holds."""
        if (bound + 1) * n > _UNARY_CELLS:
            w = _width(max(n, bound))
            return self.define("c", f"(bvadd {c or _bv(0, w)} (ite {t} {_bv(1, w)} {_bv(0, w)}))", f"(_ BitVec {w})")
        ge = c or ["false"] * (bound + 1)  # ge[k]: the count is at least k + 1
        if t == "false":
            return ge
        out = []
        for k, g in enumerate(ge):
            below = "true" if k == 0 else ge[k - 1]
            step = "false" if below == "false" else t if below == "true" else f"(and {t} {below})"
            term = g if step == "false" or g == "true" else step if g == "false" else f"(or {g} {step})"
            out.append(term if term in ("true", "false") else self.define("c", term))
        return out

    @staticmethod
    def _compare(op: str, bound: int, c: list[str] | str, n: int) -> str:
        if isinstance(c, str):
            cmp = f"({_BV_CMP[op]} {c} {_bv(bound, _width(max(n, bound)))})"
            return f"(not {cmp})" if op == "!=" else cmp
        above, reached = c[bound], "true" if bound == 0 else c[bound - 1]
        return {">": above, ">=": reached, "<=": _neg(above), "<": _neg(reached),
                "==": f"(and {reached} {_neg(above)})", "!=": f"(or {_neg(reached)} {above})"}[op]


def _neg(term: str) -> str:
    return {"true": "false", "false": "true"}.get(term, f"(not {term})")


class _Entailment:
    """Solver for "`formula` is true at the last event", to be checked under a subset of the event assumptions."""

    def __init__(self, formula: ast.Formula, events: Sequence[Event], deadline: float):
        self.encoding = enc = FactEncoding(events, formula)
        self.ctx = z3.Context()  # the global context is not thread-safe
        self.deadline = deadline
        self.solver = z3.Solver(ctx=self.ctx) if enc.name_mode == "string" else z3.SolverFor("QF_FD", ctx=self.ctx)
        goal = enc.values(formula)[-1]
        enc.defs = enc.cone(goal, deadline)
        self.solver.from_string(enc.smt2(goal))
        self.lits = [z3.Bool(name, self.ctx) for name in enc.assumptions]
        self.index = {lit.get_id(): i for i, lit in enumerate(self.lits)}
        self.gave_up = False

    def core(self, known) -> set[int] | None:
        """A subset of `known` whose facts entail a violation, or None if `known` does not (or the solver gave up)."""
        self.solver.set("timeout", max(1, int((self.deadline - time.monotonic()) * 1000)))
        asts = [self.lits[i].as_ast() for i in sorted(known)]
        # the C call skips the per-literal sort checks of Solver.check, which dominate on long traces
        result = z3.Z3_solver_check_assumptions(self.ctx.ref(), self.solver.solver, len(asts), (z3.Ast * len(asts))(*asts))
        self.gave_up = result == z3.Z3_L_UNDEF
        if result != z3.Z3_L_FALSE:
            return None
        return {self.index[c.get_id()] for c in self.solver.unsat_core()}


def minimal_counterexample(policy: CompiledPolicy, events: Sequence[Event], at_seq: int) -> tuple[int, ...]:
    """Within `BUDGET_S`; past it, a sufficient superset (at worst every event up to `at_seq`), logged."""
    return bounded_counterexample(policy, events, at_seq)[0]


def bounded_counterexample(policy: CompiledPolicy, events: Sequence[Event], at_seq: int) -> tuple[tuple[int, ...], bool]:
    """`minimal_counterexample` and whether it is minimal (False once the budget ran out)."""
    window = [e for e in events if e.seq <= at_seq]
    if not window or window[-1].seq != at_seq:
        raise ValueError(f"no event with seq {at_seq}")
    deadline = time.monotonic() + BUDGET_S
    try:
        check = _Entailment(policy.policy.formula, window, deadline)
    except TimeoutError:
        check = None
    core = check and check.core(range(len(window)))
    if core is None and (check is None or check.gave_up):
        log.warning("counterexample for %s at seq %d over budget: keeping all %d events",
                    policy.policy.name, at_seq, len(window))
        return tuple(e.seq for e in window), False
    if core is None:
        raise ValueError(f"{policy.policy.name} is not violated at seq {at_seq}")
    minimal = True
    for i in sorted(core):
        # deletion pass; an event found needed stays needed as the core shrinks
        smaller = check.core(core - {i}) if i in core else None
        if smaller is not None:
            core = smaller
        elif check.gave_up:
            log.warning("counterexample for %s at seq %d over budget: %d events, may not be minimal",
                        policy.policy.name, at_seq, len(core))
            minimal = False
            break
    return tuple(window[i].seq for i in sorted(core)), minimal
