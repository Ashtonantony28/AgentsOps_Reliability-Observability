import json
from dataclasses import replace
from unittest.mock import patch

import fakeredis.aioredis
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.policy import ast, monitor
from app.policy.checker import Monitor, Violation, check_trace, compile_policy
from app.policy.dsl import parse
from app.policy.events import Event
from app.policy.reference import evaluate
from tests.policy.fixtures import load_fixtures
from tests.policy.strategies import formulas, policies, traces, vocab_atoms

FIXTURES = load_fixtures()
vocab_policies = st.builds(ast.Policy, st.just("p"), st.just("high"), formulas(vocab_atoms, max_leaves=8))


def _compiled(ps):
    return [compile_policy(str(i), p) for i, p in enumerate(ps)]


def _stream(compiled, events, monitor=None):
    m = monitor or Monitor(compiled)
    return [v for e in events for v in m.step(e)]


def _batch(compiled, events):
    # the live Monitor leaves counterexamples to the batch check that follows it
    return [replace(v, counterexample=()) for v in check_trace(compiled, events)]


def _firsts(violations):
    return {v.policy_id: v.at_seq for v in violations}


@pytest.mark.parametrize("fx", FIXTURES, ids=lambda f: f.name)
def test_fixture_streaming_matches_check_trace(fx):
    compiled = [compile_policy(p.name, p) for p in parse(fx.source)]
    streamed = _stream(compiled, fx.events)
    assert sorted(streamed, key=lambda v: v.policy_id) == sorted(_batch(compiled, fx.events),
                                                                 key=lambda v: v.policy_id)
    assert _firsts(streamed) == {x.policy: x.at_seq for x in fx.expected}


@pytest.mark.parametrize("fx", FIXTURES, ids=lambda f: f.name)
def test_fixture_snapshot_at_every_split(fx):
    compiled = [compile_policy(p.name, p) for p in parse(fx.source)]
    whole = _stream(compiled, fx.events)
    for k in range(len(fx.events) + 1):
        m = Monitor(compiled)
        head = _stream(compiled, fx.events[:k], m)
        restored = Monitor.restore(compiled, m.snapshot())
        assert head + _stream(compiled, fx.events[k:], restored) == whole


def _agree_with_reference(ps, events):
    got = _firsts(_stream(_compiled(ps), events))
    for i, p in enumerate(ps):
        bad = evaluate(p, events)
        assert got.get(str(i)) == (bad[0] if bad else None), p


@settings(deadline=None, max_examples=2000)
@given(st.lists(vocab_policies, min_size=1, max_size=3), traces())
def test_agrees_with_reference(ps, events):
    _agree_with_reference(ps, events)


@settings(deadline=None, max_examples=1000)
@given(st.lists(policies, min_size=1, max_size=3), traces())
def test_agrees_with_reference_on_arbitrary_atoms(ps, events):
    _agree_with_reference(ps, events)


@settings(deadline=None, max_examples=300)
@given(st.lists(vocab_policies, min_size=1, max_size=3), traces(max_size=30))
def test_agrees_with_check_trace(ps, events):
    compiled = _compiled(ps)
    assert _stream(compiled, events) == sorted(_batch(compiled, events), key=lambda v: (v.at_seq, int(v.policy_id)))


@settings(deadline=None, max_examples=500)
@given(st.lists(vocab_policies, min_size=1, max_size=3), traces(), st.data())
def test_snapshot_restore_is_transparent(ps, events, data):
    compiled = _compiled(ps)
    cuts = sorted(data.draw(st.lists(st.integers(0, len(events)), max_size=3)))
    m, out, prev = Monitor(compiled), [], 0
    for c in cuts + [len(events)]:
        out += _stream(compiled, events[prev:c], m)
        m, prev = Monitor.restore(compiled, m.snapshot()), c
    assert out == _stream(compiled, events)


@settings(deadline=None, max_examples=500)
@given(st.lists(vocab_policies, min_size=1, max_size=3), traces(), st.data())
def test_failing_is_every_policy_false_at_the_event(ps, events, data):
    """`failing` lists each policy false at the stepped event, already violated or not, across restores."""
    compiled = _compiled(ps)
    bad = [set(evaluate(p, events)) for p in ps]
    cuts = set(data.draw(st.lists(st.integers(0, len(events)), max_size=3)))
    m = Monitor(compiled)
    for e in events:
        if e.seq in cuts:
            m = Monitor.restore(compiled, m.snapshot())
        m.step(e)
        assert m.failing == [str(i) for i, b in enumerate(bad) if e.seq in b]


async def test_redis_round_trip_mid_trace():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    compiled = _compiled([ast.Policy("retry_cap", "medium",
                                     ast.Count(ast.Match(status="error"), "<=", 1, by="name"))])
    events = [Event(i, "tool", n, "error") for i, n in enumerate(["a", "b", "a", "a"])]
    m = Monitor(compiled)
    assert _stream(compiled, events[:2], m) == []
    await r.set("state", m.snapshot().decode())
    restored = Monitor.restore(compiled, (await r.get("state")).encode())
    assert restored.next_seq == 2
    assert _stream(compiled, events[2:], restored) == [
        Violation("0", "retry_cap", "medium", 2, (), "retry_cap violated at seq 2")]


def test_reports_each_policy_once():
    compiled = _compiled([ast.Policy("no_x", "low", ast.Not(ast.Match(globs=("x",))))])
    m = Monitor(compiled)
    got = [m.step(Event(i, "tool", "x", "success")) for i in range(3)]
    assert [len(v) for v in got] == [1, 0, 0]
    assert m.failing == ["0"]
    assert Monitor.restore(compiled, m.snapshot()).step(Event(3, "tool", "x", "success")) == []


@pytest.mark.parametrize("seq", [1, -1, 0])
def test_rejects_out_of_order_seq(seq):
    m = Monitor(_compiled([ast.Policy("p", "low", ast.Match())]))
    if seq == 0:
        m.step(Event(0, "tool", "a", "success"))
    with pytest.raises(ValueError, match="expected seq"):
        m.step(Event(seq, "tool", "a", "success"))
    assert m.next_seq == (1 if seq == 0 else 0)


def test_restore_rejects_other_policies_and_garbage():
    a = ast.Policy("a", "low", ast.Once(ast.Match(kind="llm")))
    b = ast.Policy("b", "low", ast.Historically(ast.Match(kind="llm")))
    blob = Monitor(_compiled([a])).snapshot()
    for policies, data in [
        (_compiled([b]), blob),                        # same id, different formula
        ([compile_policy("x", a)], blob),              # different id
        (_compiled([a, b]), blob),                     # different count
        (_compiled([a]), b"not json"),
        (_compiled([a]), b"{}"),
        (_compiled([a]), json.dumps({"v": 2, "n": 0, "p": [["0", "x", False, [None]]]}).encode()),
    ]:
        with pytest.raises(ValueError):
            Monitor.restore(policies, data)


def test_restore_rejects_renamed_or_reclassified_policy():
    f = ast.Once(ast.Match(kind="llm"))
    blob = Monitor(_compiled([ast.Policy("a", "low", f)])).snapshot()
    for other in [ast.Policy("a", "high", f), ast.Policy("b", "low", f)]:
        with pytest.raises(ValueError):
            Monitor.restore(_compiled([other]), blob)
    Monitor.restore(_compiled([ast.Policy("a", "low", f, source="other text")]), blob)


def test_attr_values_stay_type_distinct():
    p = ast.Policy("p", "low", ast.Not(ast.Match(attrs=(("a", 1),))))
    q = ast.Policy("q", "low", ast.Not(ast.Match(attrs=(("a", True),))))
    compiled = _compiled([p, q])
    assert _firsts(_stream(compiled, [Event(0, "tool", "t", "success", {"a": True})])) == {"1": 0}
    blob = Monitor(_compiled([p])).snapshot()
    with pytest.raises(ValueError):
        Monitor.restore(_compiled([q]), blob)



@settings(deadline=None, max_examples=1000)
@given(st.lists(vocab_policies, min_size=1, max_size=3), traces(), st.integers(0, 3))
def test_past_the_group_bound_no_violation_is_missed(ps, events, max_groups):
    compiled = _compiled(ps)
    with patch.object(monitor, "MAX_GROUPS", max_groups):
        m = Monitor(compiled)
        for e in events:
            m.step(e)
            missed = [str(i) for i, p in enumerate(ps) if e.seq in evaluate(p, events) and str(i) not in m.failing]
            assert not missed, (e, m.snapshot())
            m = Monitor.restore(compiled, m.snapshot())


def test_past_the_group_bound_an_undecided_count_fails_the_policy():
    """A4-9: the upper bound alone says `count >= 2` holds, but the true count is 1; the policy must fail."""
    p = ast.Policy("p", "high", ast.Implies(ast.Match(kind="tool", globs=("b",)),
                                            ast.Count(ast.Match(), ">=", 2, by="name")))
    events = [Event(0, "tool", "a", "success", {}), Event(1, "tool", "b", "success", {})]
    assert evaluate(p, events) == [1]
    with patch.object(monitor, "MAX_GROUPS", 0):
        m = Monitor(_compiled([p]))
        assert [(m.step(e), list(m.failing))[1] for e in events] == [[], ["0"]]


def test_count_by_state_is_bounded_and_exact_below_the_bound():
    p = ast.Policy("p", "low", ast.Count(ast.Match(status="error"), "<=", 2, by="name"))
    n = monitor.MAX_GROUPS
    m = Monitor(_compiled([p]))
    got = [(m.step(e), m.failing)[1] for e in [Event(i, "tool", f"t{i // 2}", "error", {}) for i in range(2 * n)]]
    assert got == [[]] * (2 * n)  # each of MAX_GROUPS names errs twice: exact, within the bound
    for i in range(2 * n, 50_000):
        m.step(Event(i, "tool", f"t{i}", "error" if i % 2 else "success", {}))
    assert len(m.snapshot()) < 32 * 1024
    # past it, new names share one counter; once a count is uncertain every later event fails the policy
    assert m.failing == ["0"]


def test_count_by_values_never_collide_with_the_overflow_markers():
    p = ast.Policy("p", "low", ast.Count(ast.Match(), "<=", 1, by="name"))
    m = Monitor(_compiled([p]))
    for i, name in enumerate(["\0?", "\0", "\0\0", "a"]):
        assert m.step(Event(i, "tool", name, "success", {})) == [] and m.failing == []
