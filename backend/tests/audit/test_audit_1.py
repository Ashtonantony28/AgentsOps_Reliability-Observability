"""AUDIT-1.md findings as failing tests; each is xfail(strict=True) until its SG-A1 sub-goal fixes it."""

import pytest

import app.policy.checker as checker
from app.models import TracePolicy, TracePolicyViolation
from app.policy import reference
from app.policy.checker import check_trace, compile_policy
from app.policy.counterexample import bounded_counterexample, minimal_counterexample
from app.policy.dsl import parse
from app.policy.events import Event
from tests.api.test_event_ingest import NO_DROP, _append, _policy, _rows, ev
from tests.api.test_precheck import _check
from tests.timing import median_ms

NESTED = 'policy p: always not once (count(tool(*)) by name > 3)'
RETRY_CAP = 'policy retry_cap: always count(tool(*) and status("error")) by name <= 3'


def _named(n, last="x"):
    """n-4 distinct tool names, then 4 calls to `last`: count by name first exceeds 3 at seq n-1."""
    return [Event(i, "tool", f"t{i}", "error", {}) for i in range(n - 4)] + \
           [Event(i, "tool", last, "error", {}) for i in range(n - 4, n)]


# A1-H1 (SG-A1.1): a live `block` policy keeps blocking after its first recorded (non-blocked) violation

async def test_block_after_action_change_still_blocks(client, db, org, agent, api_key_headers):
    p = await _policy(db, org, NO_DROP, action="log")
    assert (await _append(client, api_key_headers, agent, "u", [ev(0, "drop_db")])).status_code == 202
    p.action = "block"  # an admin tightens the policy mid-trace; the source (and so the version) is unchanged
    await db.commit()
    assert (await _check(client, api_key_headers, agent, ev(1, "drop_db"), "u")).json()["allowed"] is False
    r = await _append(client, api_key_headers, agent, "u", [ev(1, "drop_db")])
    assert r.status_code == 403, r.text


async def test_block_policy_added_mid_trace_blocks_next_violation(client, db, org, agent, api_key_headers):
    assert (await _append(client, api_key_headers, agent, "u", [ev(0, "drop_db")])).status_code == 202
    await _policy(db, org, NO_DROP, action="block")
    assert (await _append(client, api_key_headers, agent, "u", [ev(1, "search")])).status_code == 202
    assert [r.at_seq for r in await _rows(db, TracePolicyViolation)] == [0]
    r = await _append(client, api_key_headers, agent, "u", [ev(2, "drop_db")])
    assert r.status_code == 403, r.text


# A1-H2: the batch verdict waits on counterexamples, whose time is unbounded outside the string theory

def test_counterexample_failure_keeps_verdicts(monkeypatch):
    (a,) = parse('policy a: always not tool("drop_db")')
    (b,) = parse(RETRY_CAP)
    events = [Event(0, "tool", "drop_db", "success", {})] + [Event(i, "tool", "x", "error", {}) for i in range(1, 5)]

    def boom(policy, events, at_seq):
        if policy.policy_id == "b":
            raise TimeoutError("counterexample budget exceeded")
        return bounded_counterexample(policy, events, at_seq)

    monkeypatch.setattr(checker, "bounded_counterexample", boom)
    got = check_trace([compile_policy("a", a), compile_policy("b", b)], events)
    assert [(v.policy_id, v.at_seq, v.minimal) for v in got] == [("a", 0, True), ("b", 4, False)]


def test_nested_count_by_counterexample_is_bounded():
    (p,) = parse(NESTED)
    events = _named(80)
    assert reference.evaluate(p, events)[:1] == [79]
    assert median_ms(lambda: check_trace([compile_policy("p", p)], events), 3) < 2000


def test_retry_cap_counterexample_meets_budget():
    (p,) = parse(RETRY_CAP)
    events = _named(200)
    (v,) = check_trace([compile_policy("p", p)], events)
    assert v.at_seq == 199 and v.counterexample == (196, 197, 198, 199)
    assert median_ms(lambda: check_trace([compile_policy("p", p)], events)) < 1000


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

def test_globbed_count_by_name_counterexample_is_minimal():
    (p,) = parse('policy p: always tool("delete_*") -> not once (count(status("error")) by name > 3)')
    events = _named(79) + [Event(79, "tool", "delete_x", "success", {})]
    (v,) = check_trace([compile_policy("p", p)], events)
    assert v.at_seq == 79 and v.counterexample == (75, 76, 77, 78, 79)
