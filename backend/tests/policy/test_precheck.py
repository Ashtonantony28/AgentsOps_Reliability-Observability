import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.policy import ast
from app.policy.checker import Monitor, Violation, compile_policy, would_violate
from app.policy.dsl import parse
from app.policy.events import Event
from app.policy.reference import truth
from tests.policy.fixtures import load_fixtures
from tests.policy.strategies import formulas, policies, traces, vocab_atoms

FIXTURES = load_fixtures()
vocab_policies = st.builds(ast.Policy, st.just("p"), st.just("high"), formulas(vocab_atoms, max_leaves=8))


def _compiled(ps):
    return [compile_policy(str(i), p) for i, p in enumerate(ps)]


def _false_at_last(ps, events):
    return {str(i) for i, p in enumerate(ps) if not truth(p.formula, events)[-1]}


def _agree_with_reference(ps, events):
    compiled = _compiled(ps)
    m = Monitor(compiled)
    for k, e in enumerate(events):
        expected = _false_at_last(ps, events[:k + 1])
        assert {v.policy_id for v in would_violate(compiled, events[:k], e)} == expected
        before = m.snapshot()
        got = m.check(e)
        assert m.snapshot() == before
        assert {v.policy_id for v in got} == expected and all(v.at_seq == k for v in got)
        m.step(e)


@settings(deadline=None, max_examples=1000)
@given(st.lists(vocab_policies, min_size=1, max_size=3), traces(max_size=30))
def test_agrees_with_reference(ps, events):
    _agree_with_reference(ps, events)


@settings(deadline=None, max_examples=500)
@given(st.lists(policies, min_size=1, max_size=3), traces(max_size=30))
def test_agrees_with_reference_on_arbitrary_atoms(ps, events):
    _agree_with_reference(ps, events)


@pytest.mark.parametrize("fx", FIXTURES, ids=lambda f: f.name)
def test_fixture_first_violation_is_predicted(fx):
    compiled = [compile_policy(p.name, p) for p in parse(fx.source)]
    for x in fx.expected:
        got = would_violate(compiled, fx.events[:x.at_seq], fx.events[x.at_seq])
        assert x.policy in {v.policy_id for v in got}


def test_reports_again_after_an_earlier_violation():
    compiled = _compiled([ast.Policy("no_x", "low", ast.Not(ast.Match(globs=("x",))))])
    events = [Event(0, "tool", "x", "success")]
    assert would_violate(compiled, events, Event(1, "tool", "x", "success")) == [
        Violation("0", "no_x", "low", 1, (), "no_x would be violated at seq 1")]
    assert would_violate(compiled, events, Event(1, "tool", "y", "success")) == []


def test_count_by_state_is_not_mutated():
    compiled = _compiled([ast.Policy("cap", "medium", ast.Count(ast.Match(status="error"), "<=", 1, by="name"))])
    m = Monitor(compiled)
    m.step(Event(0, "tool", "a", "error"))
    for _ in range(3):
        assert [v.policy_id for v in m.check(Event(1, "tool", "a", "error"))] == ["0"]
    assert m.step(Event(1, "tool", "b", "error")) == []


@pytest.mark.parametrize("seq", [0, 2, -1])
def test_check_rejects_other_seq(seq):
    m = Monitor(_compiled([ast.Policy("p", "low", ast.Match())]))
    m.step(Event(0, "tool", "a", "success"))
    with pytest.raises(ValueError, match="expected seq 1"):
        m.check(Event(seq, "tool", "a", "success"))
