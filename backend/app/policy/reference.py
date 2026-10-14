import operator
import re
from functools import lru_cache
from typing import Any, Mapping, Optional, Sequence

from app.policy.ast import (
    And, Count, Formula, Historically, Implies, Match, Not, Once, Or, Policy, Previously, Since,
)
from app.policy.events import Event

_CMP = {"<": operator.lt, "<=": operator.le, "==": operator.eq, "!=": operator.ne, ">=": operator.ge, ">": operator.gt}


@lru_cache(maxsize=1024)
def _glob(g: str) -> re.Pattern:
    return re.compile("".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in g), re.S)


def glob_match(glob: str, name: str) -> bool:
    return _glob(glob).fullmatch(name) is not None


def attr_equal(attrs: Mapping[str, Any], key: str, value: Any) -> bool:
    return key in attrs and type(attrs[key]) is type(value) and attrs[key] == value


def matches(m: Match, e: Event) -> bool:
    return (
        (m.kind is None or e.kind == m.kind)
        and (not m.globs or any(glob_match(g, e.name) for g in m.globs))
        and (m.status is None or e.status == m.status)
        and all(attr_equal(e.attrs, k, v) for k, v in m.attrs)
    )


def truth(f: Formula, events: Sequence[Event], _memo: Optional[dict] = None) -> list[bool]:
    """Truth value of `f` at every position of `events`."""
    memo = {} if _memo is None else _memo
    if id(f) not in memo:  # inlined tags share nodes; keyed by identity since True == 1 in AST equality
        memo[id(f)] = (f, _truth(f, events, lambda g: truth(g, events, memo)))
    return memo[id(f)][1]


def _truth(f: Formula, events: Sequence[Event], sub) -> list[bool]:
    n = len(events)
    if isinstance(f, Match):
        return [matches(f, e) for e in events]
    if isinstance(f, Not):
        return [not a for a in sub(f.arg)]
    if isinstance(f, (And, Or, Implies, Since)):
        a, b = sub(f.left), sub(f.right)
        if isinstance(f, And):
            return [x and y for x, y in zip(a, b)]
        if isinstance(f, Or):
            return [x or y for x, y in zip(a, b)]
        if isinstance(f, Implies):
            return [not x or y for x, y in zip(a, b)]
        return [_since(a, b, i) for i in range(n)]
    if isinstance(f, Once):
        a = sub(f.arg)
        return [any(a[: i + 1]) for i in range(n)]
    if isinstance(f, Historically):
        a = sub(f.arg)
        return [all(a[: i + 1]) for i in range(n)]
    if isinstance(f, Previously):
        a = sub(f.arg)
        return [i > 0 and a[i - 1] for i in range(n)]
    if isinstance(f, Count):
        a, cmp = sub(f.pred), _CMP[f.op]
        out = []
        for i in range(n):
            key = getattr(events[i], f.by) if f.by else None
            c = sum(1 for j in range(i + 1) if a[j] and (key is None or getattr(events[j], f.by) == key))
            out.append(cmp(c, f.bound))
        return out
    raise TypeError(f"not a formula: {f!r}")


def _since(left: list[bool], right: list[bool], i: int) -> bool:
    # ∃ j ≤ i: right[j] and ∀ k in (j, i]: left[k]. Scanning back, the first right wins; a false left ends it.
    for j in range(i, -1, -1):
        if right[j]:
            return True
        if not left[j]:
            return False
    return False


def evaluate(policy: Policy, events: Sequence[Event]) -> list[int]:
    """Seqs at which the policy's `always` body is false, in order."""
    return [e.seq for e, ok in zip(events, truth(policy.formula, events)) if not ok]
