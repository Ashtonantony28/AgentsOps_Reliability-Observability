import operator
import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.policy import encode
from app.policy.ast import (
    And, Count, Historically, Implies, Match, Not, Once, Or, Policy, Previously, Since,
)
from app.policy.dsl import parse
from app.policy.events import Event
from app.policy.reference import evaluate, glob_match, matches, truth
from tests.policy.fixtures import load_fixtures

FIXTURES = load_fixtures()
KINDS = ("tool", "llm", "retrieval", "confirmation", "custom")


def ev(seq, kind="tool", name="x", status="success", **attrs):
    return Event(seq, kind, name, status, attrs)


def trace(*specs):
    return [ev(i, *s) if isinstance(s, tuple) else ev(i, s) for i, s in enumerate(specs)]


def bits(s):
    """Events whose truth under A / B is given by '1'/'0' strings."""
    return [ev(i, "tool", ("a" if x == "1" else "") + ("b" if y == "1" else ""))
            for i, (x, y) in enumerate(zip(*s.split()))] if s else []


A = Match(globs=("a*",))
B = Match(globs=("*b",))


@pytest.mark.parametrize("fx", FIXTURES, ids=lambda f: f.name)
def test_fixture_first_violation(fx):
    expected = {x.policy: x.at_seq for x in fx.expected}
    policies = parse(fx.source)
    assert set(expected) <= {p.name for p in policies}
    for p in policies:
        got = evaluate(p, fx.events)
        assert got[:1] == ([expected[p.name]] if p.name in expected else [])


@pytest.mark.parametrize("glob,name,ok", [
    ("*", "", True), ("a*", "a", True), ("*a*", "a", True), ("a*b", "ab", True),
    ("?", "", False), ("?", "a", True), ("?", "ab", False), ("?", "é", True), ("?", "日", True),
    ("??", "日本", True), ("?", "\U0001F600", True),
    ("abc", "abc", True), ("abc", "xabc", False), ("abc", "abcx", False), ("abc", "ABC", False),
    ("a.c", "abc", False), ("a.c", "a.c", True), ("a+", "aa", False), ("a+", "a+", True),
    ("f(x)", "f(x)", True), ("a$", "a$", True), ("^a", "^a", True), ("a|b", "a", False),
    ("a{2}", "aa", False), ("*", "a\nb", True), ("a?b", "a\nb", True), ("a*", "a\n", True),
])
def test_glob(glob, name, ok):
    assert glob_match(glob, name) is ok


@settings(max_examples=2000)
@given(st.text("ab*?", max_size=6), st.text("abx", max_size=8))
def test_glob_matchers_agree_with_naive(glob, name):
    assert glob_match(glob, name) is encode.glob_match(glob, name) is _glob(glob, name)


@settings(max_examples=2000)
@given(st.lists(st.text("ab*?", min_size=1, max_size=5), min_size=1, max_size=4), st.text("abx", max_size=8))
def test_compiled_glob_lists_agree_with_naive(globs, name):
    """SG-A9.1: the plan's glob-list matcher (sets and prefix/suffix tuples) is any() of the naive matcher."""
    assert encode.compile_globs(globs)(name) is any(_glob(g, name) for g in globs)


@pytest.mark.parametrize("match", [glob_match, encode.glob_match])
def test_glob_does_not_backtrack(match):
    glob = "*a" * 10 + "b"
    t = time.perf_counter()
    assert not match(glob, "a" * 255) and match(glob, "a" * 254 + "b")
    assert time.perf_counter() - t < 0.1


@pytest.mark.parametrize("m,e,ok", [
    (Match(), ev(0, "llm", "anything", "error"), True),
    (Match(kind="tool"), ev(0, "tool"), True),
    (Match(kind="tool"), ev(0, "llm"), False),
    (Match(globs=("a*", "b*")), ev(0, name="bx"), True),
    (Match(globs=("a*", "b*")), ev(0, name="cx"), False),
    (Match(kind="llm", globs=("x",)), ev(0, "tool", "x"), False),
    (Match(status="error"), ev(0, status="error"), True),
    (Match(status="error"), ev(0, status="success"), False),
    (Match(attrs=(("k", True),)), ev(0, k=True), True),
    (Match(attrs=(("k", True),)), ev(0, k=1), False),
    (Match(attrs=(("k", 1),)), ev(0, k=True), False),
    (Match(attrs=(("k", 2),)), ev(0, k="2"), False),
    (Match(attrs=(("k", "2"),)), ev(0, k=2), False),
    (Match(attrs=(("k", 2),)), ev(0, k=2.0), False),
    (Match(attrs=(("k", 2),)), ev(0, k=2), True),
    (Match(attrs=(("k", 2),)), ev(0, j=2), False),
    (Match(attrs=(("k", None),)), ev(0), False),
    (Match(attrs=(("k", 1), ("j", "v"))), ev(0, k=1, j="v"), True),
    (Match(attrs=(("k", 1), ("j", "v"))), ev(0, k=1, j="w"), False),
])
def test_match(m, e, ok):
    assert matches(m, e) is ok


@pytest.mark.parametrize("f,s,want", [
    (Not(A), "1010 0000", [0, 1, 0, 1]),
    (And(A, B), "1100 1010", [1, 0, 0, 0]),
    (Or(A, B), "1100 1010", [1, 1, 1, 0]),
    (Implies(A, B), "1100 1010", [1, 0, 1, 1]),
    (Once(A), "0010 0000", [0, 0, 1, 1]),
    (Historically(A), "1101 0000", [1, 1, 0, 0]),
    (Previously(A), "1101 0000", [0, 1, 1, 0]),
    (Previously(A), "1 0", [0]),
    (Since(A, B), "0000 0000", [0, 0, 0, 0]),
    (Since(A, B), "0110 1000", [1, 1, 1, 0]),
    (Since(A, B), "0000 0100", [0, 1, 0, 0]),
    (Since(A, B), "1111 0100", [0, 1, 1, 1]),
    (Since(A, B), "0001 1010", [1, 0, 1, 1]),
    (Since(A, B), "1111 0000", [0, 0, 0, 0]),
])
def test_operators(f, s, want):
    assert truth(f, bits(s)) == [bool(x) for x in want]


@pytest.mark.parametrize("op,bound,want", [
    ("<", 2, [1, 0, 0, 0]), ("<=", 2, [1, 1, 1, 0]), ("==", 2, [0, 1, 1, 0]),
    ("!=", 2, [1, 0, 0, 1]), (">=", 2, [0, 1, 1, 1]), (">", 2, [0, 0, 0, 1]),
    ("==", 0, [0, 0, 0, 0]), (">=", 0, [1, 1, 1, 1]),
])
def test_count_ops(op, bound, want):
    assert truth(Count(A, op, bound), bits("1101 0000")) == [bool(x) for x in want]


def test_count_by():
    events = trace(("tool", "a"), ("llm", "a"), ("tool", "b"), ("tool", "a"), ("llm", "c"))
    assert truth(Count(Match(), "==", 2, by="name"), events) == [False, True, False, False, False]
    assert truth(Count(Match(), "<=", 1, by="name"), events) == [True, False, True, False, True]
    assert truth(Count(Match(), "==", 2, by="kind"), events) == [False, False, True, False, True]
    assert truth(Count(Match(globs=("a",)), "==", 1, by="kind"), events) == [True, True, True, False, True]


def test_shared_tag_nodes_are_evaluated_once():
    f = Match(kind="tool", globs=("a",))
    for _ in range(39):  # what tags inline to; parse caps the expanded size
        f = Or(f, Not(f))
    p = Policy("p", "medium", f)
    assert evaluate(p, trace("a", ("llm", "b"))) == []


def test_unknown_node_raises():
    with pytest.raises(TypeError):
        truth("tool", [ev(0)])


def test_empty_trace():
    for f in (Match(), Not(Match()), Previously(Match()), Count(Match(), "<", 0), Since(A, B)):
        assert truth(f, []) == []
        assert evaluate(Policy("p", "high", f), []) == []


def test_evaluate_returns_seqs_not_indexes():
    events = [ev(5 + i, name=n) for i, n in enumerate(["ok", "bad", "ok", "bad"])]
    assert evaluate(Policy("p", "low", Not(Match(globs=("bad",)))), events) == [6, 8]
    assert evaluate(Policy("p", "low", Previously(Match())), events) == [5]
    assert evaluate(Policy("p", "low", Match()), events) == []


# Naive oracle, written literally from the semantics table.
_OPS = {"<": operator.lt, "<=": operator.le, "==": operator.eq, "!=": operator.ne, ">=": operator.ge, ">": operator.gt}


def _glob(g, s):
    if not g:
        return not s
    if g[0] == "*":
        return any(_glob(g[1:], s[k:]) for k in range(len(s) + 1))
    return bool(s) and (g[0] == "?" or g[0] == s[0]) and _glob(g[1:], s[1:])


def naive(f, t, i):
    e = t[i]
    if isinstance(f, Match):
        return ((f.kind is None or e.kind == f.kind)
                and (not f.globs or any(_glob(g, e.name) for g in f.globs))
                and (f.status is None or e.status == f.status)
                and all(k in e.attrs and type(e.attrs[k]) == type(v) and e.attrs[k] == v for k, v in f.attrs))
    if isinstance(f, Not):
        return not naive(f.arg, t, i)
    if isinstance(f, And):
        return naive(f.left, t, i) and naive(f.right, t, i)
    if isinstance(f, Or):
        return naive(f.left, t, i) or naive(f.right, t, i)
    if isinstance(f, Implies):
        return not naive(f.left, t, i) or naive(f.right, t, i)
    if isinstance(f, Once):
        return any(naive(f.arg, t, j) for j in range(i + 1))
    if isinstance(f, Historically):
        return all(naive(f.arg, t, j) for j in range(i + 1))
    if isinstance(f, Previously):
        return i > 0 and naive(f.arg, t, i - 1)
    if isinstance(f, Since):
        return any(naive(f.right, t, j) and all(naive(f.left, t, k) for k in range(j + 1, i + 1))
                   for j in range(i + 1))
    if isinstance(f, Count):
        n = len({j for j in range(i + 1)
                 if naive(f.pred, t, j) and (f.by is None or getattr(t[j], f.by) == getattr(e, f.by))})
        return _OPS[f.op](n, f.bound)
    raise TypeError(f)


NAMES = ("read", "rm", "rm_all", "日")
ATTR_VALUES = (True, 1, "1", 1.0, False, 0)

events_st = st.lists(
    st.tuples(st.sampled_from(KINDS), st.sampled_from(NAMES), st.sampled_from(("success", "error")),
              st.dictionaries(st.sampled_from(("k", "j")), st.sampled_from(ATTR_VALUES), max_size=2)),
    max_size=8,
).map(lambda xs: [Event(3 + i, k, n, s, a) for i, (k, n, s, a) in enumerate(xs)])

match_st = st.builds(
    Match,
    kind=st.none() | st.sampled_from(KINDS),
    globs=st.lists(st.sampled_from(("*", "rm*", "r?", "?", "read", "*_*", "日")), max_size=2).map(tuple),
    status=st.none() | st.sampled_from(("success", "error")),
    attrs=st.lists(st.tuples(st.sampled_from(("k", "j")), st.sampled_from(ATTR_VALUES)), max_size=1).map(tuple),
)


def _extend(sub):
    return st.one_of(
        st.builds(Not, sub), st.builds(Once, sub), st.builds(Historically, sub), st.builds(Previously, sub),
        st.builds(And, sub, sub), st.builds(Or, sub, sub), st.builds(Implies, sub, sub), st.builds(Since, sub, sub),
        st.builds(Count, sub, st.sampled_from(tuple(_OPS)), st.integers(0, 4),
                  st.sampled_from((None, "name", "kind"))),
    )


formula_st = match_st | _extend(match_st | _extend(match_st | _extend(match_st)))


@settings(max_examples=300, deadline=None)
@given(formula_st, events_st)
def test_truth_matches_naive_oracle(f, t):
    assert truth(f, t) == [naive(f, t, i) for i in range(len(t))]
    assert evaluate(Policy("p", "info", f), t) == [e.seq for i, e in enumerate(t) if not naive(f, t, i)]
