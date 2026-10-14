import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import z3
from hypothesis import given, settings

from app.policy.ast import And, Count, Historically, Implies, Match, Not, Once, Or, Policy, Previously, Since
from app.policy.checker import Violation, check_trace, compile_policy
from app.policy.dsl import parse
from app.policy.encode import TraceEncoding, attr_equal, first_violations, glob_match, matches
from app.policy.events import Event
from tests.policy.fixture_policies import BODIES, attr, event, fixture_policies, status, tool
from tests.policy.fixtures import load_fixtures
from tests.policy.strategies import formulas, traces, vocab_atoms

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import bench_policy  # noqa: E402

FIXTURES = load_fixtures()


def ev(i, name="x", kind="tool", st="success", **attrs):
    return Event(i, kind, name, st, attrs)


def firsts(formula, events):
    return first_violations([formula], events)[0]


@pytest.mark.parametrize("fx", FIXTURES, ids=lambda f: f.name)
def test_fixture_verdicts(fx):
    ps = fixture_policies(fx)
    assert len(ps) == fx.source.count("\npolicy ") + fx.source.startswith("policy ")
    got = check_trace([compile_policy(p.name, p) for p in ps], fx.events)
    assert sorted((v.policy_name, v.at_seq) for v in got) == sorted((x.policy, x.at_seq) for x in fx.expected)


@pytest.mark.parametrize("fx", FIXTURES, ids=lambda f: f.name)
def test_fixture_asts_match_parser(fx):
    parsed = parse(fx.source)
    assert parsed == fixture_policies(fx)
    assert [repr(p.formula) for p in parsed] == [repr(p.formula) for p in fixture_policies(fx)]


def test_violation_fields():
    p = Policy("confirm", "high", Implies(tool("delete_*"), Once(event("confirmation"))))
    events = [ev(0, "delete_a"), ev(1, "ok", "confirmation"), ev(2, "delete_b")]
    assert check_trace([compile_policy("id-1", p)], events) == [
        Violation("id-1", "confirm", "high", 0, (0,), "confirm violated at seq 0")]
    assert check_trace([compile_policy("id-1", p)], events[1:]) == []
    assert check_trace([], events) == []


@pytest.mark.parametrize("glob,name,ok", [
    ("a*", "a", True), ("a*", "ba", False), ("?", "é", True), ("?", "", False), ("??", "🚀", False),
    ("a.b", "a.b", True), ("a.b", "axb", False), ("a+", "aa", False), ("(x)", "(x)", True), ("*_?", "a_\n", True),
    ("A*", "a", False), ("*", "", True), ("a?c", "a\nc", True), ("$", "$", True), ("^a", "^a", True),
])
def test_glob(glob, name, ok):
    assert glob_match(glob, name) is ok


@pytest.mark.parametrize("actual,expected,ok", [
    (True, True, True), (1, True, False), (True, 1, False), (0, False, False), ("2", 2, False), (2, 2, True),
    (2.0, 2, False), ("prod", "prod", True), (None, False, False),
])
def test_attr_equal_is_type_strict(actual, expected, ok):
    assert attr_equal(actual, expected) is ok


def test_matches():
    e = ev(0, "delete_user", st="error", env="prod", n=1)
    assert matches(Match(), e)
    assert matches(Match(kind="tool", globs=("get_*", "delete_*")), e)
    assert not matches(Match(kind="llm"), e)
    assert not matches(Match(kind="tool", globs=("get_*",)), e)
    assert matches(Match(status="error"), e) and not matches(Match(status="success"), e)
    assert matches(Match(attrs=(("env", "prod"), ("n", 1))), e)
    assert not matches(Match(attrs=(("n", True),)), e)
    assert not matches(Match(attrs=(("missing", False),)), e)


def test_attr_atoms_with_equal_python_values_stay_distinct():
    # Match(attrs=(("a", 1),)) == Match(attrs=(("a", True),)) as dataclasses; the encoder must not merge them
    events = [ev(0, a=True), ev(1, a=1)]
    assert first_violations([Not(attr("a", 1)), Not(attr("a", True))], events) == [1, 0]
    assert first_violations([Not(attr("a", True)), Not(attr("a", 1))], events) == [0, 1]


@pytest.mark.parametrize("formula,names,expected", [
    (Once(tool("a")), "ba", 0),
    (Once(tool("a")), "ab", None),
    (Historically(tool("a")), "aab", 2),
    (Historically(tool("a")), "aaa", None),
    (Not(Previously(tool("a"))), "ab", 1),
    (Previously(tool("a")), "aa", 0),
    (Previously(Match()), "a", 0),
    (Implies(tool("c"), Previously(Previously(tool("a")))), "abc", None),
    (Implies(tool("c"), Previously(Previously(tool("a")))), "bac", 2),
    (Since(tool("b"), tool("a")), "abbb", None),
    (Since(tool("b"), tool("a")), "abcb", 2),
    (Since(tool("b"), tool("a")), "ba", 0),
    (Not(Since(Match(), tool("a"))), "ba", 1),
    (Count(tool("a"), "<", 2), "aba", 2),
    (Count(tool("a"), "<=", 0), "ba", 1),
    (Count(tool("a"), "==", 1), "bab", 0),
    (Count(tool("a"), "!=", 1), "bab", 1),
    (Count(tool("a"), ">=", 1), "ab", None),
    (Count(tool("a"), ">", 1), "aa", 0),
    (Count(tool("a", "b"), "<", 2, by="name"), "abab", 2),
    (Count(tool("a", "b"), "<", 2, by="name"), "abcd", None),
    (Count(Once(tool("a")), "<", 3), "abbb", 2),
    (Or(tool("a"), And(tool("b"), Not(tool("b")))), "ab", 1),
])
def test_operators(formula, names, expected):
    assert firsts(formula, [ev(i, n) for i, n in enumerate(names)]) == expected


def test_count_by_kind_groups_by_current_kind():
    events = [ev(0, kind="llm", st="error"), ev(1, kind="tool", st="error"), ev(2, kind="llm", st="error")]
    assert firsts(Count(status("error"), "<", 2, by="kind"), events) == 2
    assert firsts(Count(status("error"), "<", 2), events) == 1


def test_empty_trace_violates_nothing():
    assert first_violations([Count(Match(), ">=", 1), Previously(Match())], []) == [None, None]


def test_unknown_node_rejected():
    with pytest.raises(TypeError):
        first_violations([object()], [ev(0)])


def test_reports_seq_of_event():
    assert firsts(Not(tool("b")), [ev(10, "a"), ev(11, "b")]) == 11


def test_shared_subformulas_are_encoded_once():
    events = bench_policy.synthetic_trace(50)
    f = Implies(tool("delete_*"), Once(event("confirmation")))
    one, two = TraceEncoding(events), TraceEncoding(events)
    one.values(f)
    two.values(f), two.values(Implies(tool("delete_*"), Once(event("confirmation"))))
    two.values(Once(event("confirmation")))
    assert one.smt2() == two.smt2()


def _node_count(n: int) -> int:
    enc = TraceEncoding(bench_policy.synthetic_trace(n))
    for p in bench_policy.synthetic_policies():
        enc.values(p.formula)
    seen, stack = set(), list(z3.parse_smt2_string(enc.smt2()))
    while stack:
        t = stack.pop()
        if t.get_id() not in seen:
            seen.add(t.get_id())
            stack.extend(t.children())
    return len(seen)


def test_encoding_size_is_linear():
    small, large = _node_count(100), _node_count(1000)
    assert large <= 12 * small, (small, large)


def test_long_trace():
    events = bench_policy.synthetic_trace(5000)
    got = first_violations([p.formula for p in bench_policy.synthetic_policies()], events)
    assert len(got) == bench_policy.N_POLICIES


@settings(deadline=None, max_examples=100)
@given(formulas(vocab_atoms, max_leaves=8), traces(max_size=20))
def test_violation_is_prefix_closed(f, events):
    # past-time: the first violation depends only on the prefix up to it
    at = firsts(f, events)
    for k in range(len(events) + 1):
        expected = at if at is not None and at < k else None
        assert firsts(f, events[:k]) == expected


@settings(deadline=None, max_examples=100)
@given(formulas(vocab_atoms, max_leaves=8), formulas(vocab_atoms, max_leaves=8), traces(max_size=20))
def test_batching_policies_does_not_change_verdicts(f, g, events):
    assert first_violations([f, g, f], events) == [firsts(f, events), firsts(g, events), firsts(f, events)]


def test_bodies_cover_every_fixture_policy():
    used = {repr(p.formula) for fx in FIXTURES for p in fixture_policies(fx)}
    assert used == {repr(b) for b in BODIES.values()}


def test_concurrent_checks_are_independent():
    events = bench_policy.synthetic_trace(100)
    fs = [p.formula for p in bench_policy.synthetic_policies()]
    expected = first_violations(fs, events)
    with ThreadPoolExecutor(4) as pool:
        assert list(pool.map(lambda _: first_violations(fs, events), range(16))) == [expected] * 16
