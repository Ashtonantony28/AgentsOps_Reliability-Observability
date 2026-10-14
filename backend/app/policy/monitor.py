"""Event-by-event evaluation of past-time formulas with constant state per node (PLAN.md, design decision 1).

Same recurrences as the linear Z3 encoding, evaluated on concrete values so a live step costs microseconds.
"""
import hashlib
import operator
from functools import lru_cache
from typing import Any

from app.policy import ast
from app.policy.encode import _match_key, matches
from app.policy.events import Event

_CMP = {"<": operator.lt, "<=": operator.le, "==": operator.eq, "!=": operator.ne, ">=": operator.ge,
        ">": operator.gt}
_INIT = {"Previously": False, "Once": False, "Historically": True, "Since": False}


@lru_cache(maxsize=4096)
def _plan(key: str, policy: ast.Policy) -> "Plan":
    return Plan(policy.formula, hashlib.sha256(key.encode()).hexdigest()[:16])


def plan_for(policy: ast.Policy) -> "Plan":
    """Plans are immutable and cached; the digest changes with the formula, name or severity."""
    return _plan(repr((policy.name, policy.severity, policy.formula)), policy)


class Plan:
    """`formula` flattened to nodes in evaluation order (children first), shared subformulas evaluated once."""

    def __init__(self, formula: ast.Formula, digest: str):
        self.nodes: list[tuple[str, Any, tuple[int, ...]]] = []
        self._index: dict[Any, int] = {}
        self.root = self._add(formula)[1]
        self.digest = digest

    def _add(self, f: ast.Formula) -> tuple[Any, int]:
        op = type(f).__name__
        if isinstance(f, ast.Match):
            key, payload, kids = _match_key(f), f, ()
        elif isinstance(f, ast.Count):
            k, i = self._add(f.pred)
            key, payload, kids = (op, k, f.op, f.bound, f.by), f, (i,)
        else:
            subs = [self._add(f.arg)] if hasattr(f, "arg") else [self._add(f.left), self._add(f.right)]
            key, payload, kids = (op, *(k for k, _ in subs)), None, tuple(i for _, i in subs)
        if key not in self._index:
            self._index[key] = len(self.nodes)
            self.nodes.append((op, payload, kids))
        return key, self._index[key]

    def initial(self) -> list:
        return [({} if f.by else 0) if op == "Count" else _INIT.get(op) for op, f, _ in self.nodes]

    def step(self, state: list, e: Event) -> bool:
        """Advances `state` in place by event `e`; returns the formula's value at `e`."""
        v: list[bool] = [False] * len(self.nodes)
        for k, (op, f, kids) in enumerate(self.nodes):
            if op == "Match":
                v[k] = matches(f, e)
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
                if f.by is None:
                    c = state[k] = state[k] + v[kids[0]]
                else:
                    g = getattr(e, f.by)
                    c = state[k][g] = state[k].get(g, 0) + v[kids[0]]
                v[k] = _CMP[f.op](c, f.bound)
        return v[self.root]
