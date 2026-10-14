import json

import fakeredis.aioredis
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.policy import ast
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


def _firsts(violations):
    return {v.policy_id: v.at_seq for v in violations}


@pytest.mark.parametrize("fx", FIXTURES, ids=lambda f: f.name)
def test_fixture_streaming_matches_check_trace(fx):
    compiled = [compile_policy(p.name, p) for p in parse(fx.source)]
    streamed = _stream(compiled, fx.events)
    assert sorted(streamed, key=lambda v: v.policy_id) == sorted(check_trace(compiled, fx.events),
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
    assert _stream(compiled, events) == sorted(check_trace(compiled, events), key=lambda v: (v.at_seq, int(v.policy_id)))


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
