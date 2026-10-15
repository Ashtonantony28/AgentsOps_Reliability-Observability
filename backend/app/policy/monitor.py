"""Event-by-event evaluation of past-time formulas with constant state per node (PLAN.md, design decision 1).

Same recurrences as the linear Z3 encoding, evaluated on concrete values so a live step costs microseconds.
"""
import hashlib
import operator
import threading
from array import array
from itertools import repeat
from typing import Any, Callable

from app.policy import ast
from app.policy.encode import Dag, compile_globs, glob_cost, matches
from app.policy.events import Event

_CMP = {"<": operator.lt, "<=": operator.le, "==": operator.eq, "!=": operator.ne, ">=": operator.ge,
        ">": operator.gt}
_INIT = {"Previously": False, "Once": False, "Historically": True, "Since": False}
MAX_GROUPS = 1000  # per `count ... by` node
MAX_MONITOR_GROUPS = 10_000  # split evenly over a monitor's nodes, so its snapshot stays a few hundred KB
MAX_KEY_CHARS = 32  # longer values are kept as a digest
_OVERFLOW, _UNSURE = "\0", "\0?"


def _group_key(g: str) -> str:
    if len(g) > MAX_KEY_CHARS:
        return "\0#" + hashlib.blake2b(g.encode(), digest_size=12).hexdigest()
    return "\0" + g if g.startswith("\0") else g  # keeps client values clear of the markers and digests


def _count_group(groups: dict, g: str, hit: bool, max_groups: float) -> tuple[int, int]:
    """Bounds on the count of `g` after this event: exact for the first `max_groups` values seen with a hit
    (event names are client-chosen); later values share one overflow counter, so at least this event, at most
    every overflow hit."""
    g = _group_key(g)
    if g in groups:
        c = groups[g] = groups[g] + hit
        return c, c
    if hit and len(groups) < max_groups:
        groups[g] = 1
        return 1, 1
    hi = groups.get(_OVERFLOW, 0) + hit
    if hit:
        groups[_OVERFLOW] = hi
    return int(hit), hi


_plans: dict[int, tuple[ast.Policy, "Plan"]] = {}
_PLANS_MAX = 4096
_plans_lock = threading.Lock()


def plan_for(policy: ast.Policy) -> "Plan":
    """Plans are immutable and cached per policy object; the digest changes with the formula, name or severity."""
    hit = _plans.get(id(policy))
    if hit is not None and hit[0] is policy:
        return hit[1]
    plan = Plan(policy)
    with _plans_lock:  # monitors are built in worker threads too
        if len(_plans) >= _PLANS_MAX:
            _plans.pop(next(iter(_plans)), None)
        _plans[id(policy)] = (policy, plan)  # holding `policy` keeps its id from being reused while cached
    return plan


class Plan:
    """`formula` flattened to nodes in evaluation order (children first), shared subformulas evaluated once."""

    def __init__(self, policy: ast.Policy):
        dag = Dag()
        self.root = dag.add(policy.formula)
        self.nodes: list[tuple[str, Any, tuple[int, ...]]] = [
            (type(f).__name__, f if isinstance(f, (ast.Match, ast.Count)) else None, kids)
            for f, kids in zip(dag.nodes, dag.kids)]
        self.count_by = [k for k, (op, f, _) in enumerate(self.nodes) if op == "Count" and f.by]
        # `checker.work` units per event: one per node, which covers a Match's first simple glob
        self.cost = len(self.nodes) + sum(max(sum(map(glob_cost, set(f.globs))) - 1, 0)
                                          for op, f, _ in self.nodes if op == "Match")
        self.digest = hashlib.sha256(repr((policy.name, policy.severity, list(dag.keys))).encode()).hexdigest()[:16]
        self._globs: dict[int, Callable[[str], bool]] = {}  # compiled on first step: plans built only for `cost` stay small

    def _match(self, k: int, f: ast.Match, e: Event) -> bool:
        globs = self._globs.get(k)
        if globs is None:
            globs = self._globs[k] = compile_globs(f.globs)
        return matches(f, e, globs)

    def initial(self, batch: bool = False) -> list:
        """`batch`: state for `step` with `ids`, never stored."""
        return [((array("i") if batch else {}) if f.by else 0) if op == "Count" else _INIT.get(op)
                for op, f, _ in self.nodes]

    def step(self, state: list, e: Event, max_groups: float | None = None, memo: dict | None = None,
             ids: dict | None = None) -> bool:
        """Advances `state` in place by event `e`; returns the formula's value at `e`. `max_groups` bounds the
        values each `count ... by` node counts exactly. With `ids` (a value -> index map shared by one trace's
        plans) and a `batch` state, every value is counted exactly in an array. `memo` keeps attr-free atoms'
        values by (node, kind, name, status) across steps, for replays where names repeat."""
        v: list[bool] = [False] * len(self.nodes)
        max_groups = MAX_GROUPS if max_groups is None else max_groups
        for k, (op, f, kids) in enumerate(self.nodes):
            if op == "Match":
                if memo is None or f.attrs:
                    v[k] = self._match(k, f, e)
                else:
                    key = (k, e.kind, e.name, e.status)
                    hit = memo.get(key)
                    v[k] = memo[key] = self._match(k, f, e) if hit is None else hit
            elif op == "Not":
                v[k] = not v[kids[0]]
            elif op == "And":
                v[k] = v[kids[0]] and v[kids[1]]
            elif op == "Or":
                v[k] = v[kids[0]] or v[kids[1]]
            elif op == "Implies":
                v[k] = not v[kids[0]] or v[kids[1]]
            elif op == "Previously":
                v[k], state[k] = state[k], v[kids[0]]
            elif op == "Once":
                v[k] = state[k] = v[kids[0]] or state[k]
            elif op == "Historically":
                v[k] = state[k] = v[kids[0]] and state[k]
            elif op == "Since":
                v[k] = state[k] = v[kids[1]] or (v[kids[0]] and state[k])
            elif op == "Count":
                cmp = _CMP[f.op]
                if f.by is None:
                    state[k] += v[kids[0]]
                    v[k] = cmp(state[k], f.bound)
                elif ids is not None:
                    counts, g = state[k], ids.setdefault(getattr(e, f.by), len(ids))
                    if v[kids[0]]:
                        if g >= len(counts):
                            counts.extend(repeat(0, g + 1 - len(counts)))
                        counts[g] += 1
                    v[k] = cmp(counts[g] if g < len(counts) else 0, f.bound)
                else:
                    lo, hi = _count_group(state[k], getattr(e, f.by), v[kids[0]], max_groups)
                    v[k] = cmp(hi, f.bound)
                    if lo != hi and len({v[k], cmp(lo, f.bound), cmp(min(max(f.bound, lo), hi), f.bound)}) > 1:
                        state[k][_UNSURE] = 1
        # once a count is uncertain the policy is false at every later event: never a missed violation
        return v[self.root] and (ids is not None or not any(_UNSURE in state[k] for k in self.count_by))
