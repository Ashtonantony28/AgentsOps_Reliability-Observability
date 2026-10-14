"""AUDIT-1.md findings as failing tests; each is xfail(strict=True) until its SG-A1 sub-goal fixes it."""
import time

import pytest

import app.policy.checker as checker
from app.models import TracePolicy, TracePolicyViolation
from app.policy import reference
from app.policy.checker import check_trace, compile_policy
from app.policy.counterexample import minimal_counterexample
from app.policy.dsl import parse
from app.policy.events import Event
from tests.api.test_event_ingest import NO_DROP, _append, _policy, _rows, ev
from tests.api.test_precheck import _check

NESTED = 'policy p: always not once (count(tool(*)) by name > 3)'
RETRY_CAP = 'policy retry_cap: always count(tool(*) and status("error")) by name <= 3'


def _named(n, last="x"):
    """n-4 distinct tool names, then 4 calls to `last`: count by name first exceeds 3 at seq n-1."""
    return [Event(i, "tool", f"t{i}", "error", {}) for i in range(n - 4)] + \
           [Event(i, "tool", last, "error", {}) for i in range(n - 4, n)]


# A1-H1: a live `block` policy stops blocking after its first recorded (non-blocked) violation

@pytest.mark.xfail(strict=True, reason="A1-H1: Monitor.step reports a policy once, so later events are not blocked")
async def test_block_after_action_change_still_blocks(client, db, org, agent, api_key_headers):
    p = await _policy(db, org, NO_DROP, action="log")
    assert (await _append(client, api_key_headers, agent, "u", [ev(0, "drop_db")])).status_code == 202
    p.action = "block"  # an admin tightens the policy mid-trace; the source (and so the version) is unchanged
    await db.commit()
    assert (await _check(client, api_key_headers, agent, ev(1, "drop_db"), "u")).json()["allowed"] is False
    r = await _append(client, api_key_headers, agent, "u", [ev(1, "drop_db")])
    assert r.status_code == 403, r.text


@pytest.mark.xfail(strict=True, reason="A1-H1: a violation found while rebuilding marks the block policy done")
async def test_block_policy_added_mid_trace_blocks_next_violation(client, db, org, agent, api_key_headers):
    assert (await _append(client, api_key_headers, agent, "u", [ev(0, "drop_db")])).status_code == 202
    await _policy(db, org, NO_DROP, action="block")
    assert (await _append(client, api_key_headers, agent, "u", [ev(1, "search")])).status_code == 202
    assert [r.at_seq for r in await _rows(db, TracePolicyViolation)] == [0]
    r = await _append(client, api_key_headers, agent, "u", [ev(2, "drop_db")])
    assert r.status_code == 403, r.text


# A1-H2: the batch verdict waits on counterexamples, whose time is unbounded outside the string theory

@pytest.mark.xfail(strict=True, reason="A1-H2: one failing counterexample drops every violation of the trace")
def test_counterexample_failure_keeps_verdicts(monkeypatch):
    (a,) = parse('policy a: always not tool("drop_db")')
    (b,) = parse(RETRY_CAP)
    events = [Event(0, "tool", "drop_db", "success", {})] + [Event(i, "tool", "x", "error", {}) for i in range(1, 5)]

    def boom(policy, events, at_seq):
        if policy.policy_id == "b":
            raise TimeoutError("counterexample budget exceeded")
        return minimal_counterexample(policy, events, at_seq)

    monkeypatch.setattr(checker, "minimal_counterexample", boom)
    got = check_trace([compile_policy("a", a), compile_policy("b", b)], events)
    assert [(v.policy_id, v.at_seq) for v in got] == [("a", 0), ("b", 4)]


@pytest.mark.xfail(strict=True, reason="A1-H2: nested `count by name` counterexample is exponential (80 events ~11 s)")
def test_nested_count_by_counterexample_is_bounded():
    (p,) = parse(NESTED)
    events = _named(80)
    assert reference.evaluate(p, events)[:1] == [79]
    start = time.monotonic()
    check_trace([compile_policy("p", p)], events)
    assert time.monotonic() - start < 2.0


@pytest.mark.xfail(strict=True, reason="A1-H2: top-level `count by name` counterexample at 200 events ~7 s, budget 500 ms")
def test_retry_cap_counterexample_meets_budget():
    (p,) = parse(RETRY_CAP)
    events = _named(200)
    start = time.monotonic()
    (v,) = check_trace([compile_policy("p", p)], events)
    assert v.at_seq == 199 and v.counterexample == (196, 197, 198, 199)
    assert time.monotonic() - start < 1.0


# A1-L1: unconstrained events in a counterexample range over the 5 kinds plus the observed ones only

@pytest.mark.xfail(strict=True, reason="A1-L1: a kind outside the trace and the 5 EventIn kinds is not considered")
def test_counterexample_holds_for_unseen_kind():
    (p,) = parse('policy p: always count(event(*)) by kind != 2')
    kinds = ["tool", "llm", "retrieval", "confirmation", "custom"]
    events = [Event(i, k, "n", "success", {}) for i, k in enumerate(kinds)] + [Event(5, "tool", "n", "success", {})]
    at = reference.evaluate(p, events)[0]
    cx = minimal_counterexample(compile_policy("p", p), events, at)
    completed = [e if e.seq in cx else Event(e.seq, "span", e.name, e.status, {}) for e in events]
    assert not reference.truth(p.formula, completed)[at]


# A1-L2: `count by name` plus globs falls back to the 1 s string-theory budget and keeps every unresolved event

@pytest.mark.xfail(strict=True, reason="A1-L2: string-theory budget runs out; all 80 events kept where 5 suffice")
def test_globbed_count_by_name_counterexample_is_minimal():
    (p,) = parse('policy p: always tool("delete_*") -> not once (count(status("error")) by name > 3)')
    events = _named(79) + [Event(79, "tool", "delete_x", "success", {})]
    (v,) = check_trace([compile_policy("p", p)], events)
    assert v.at_seq == 79 and v.counterexample == (75, 76, 77, 78, 79)
