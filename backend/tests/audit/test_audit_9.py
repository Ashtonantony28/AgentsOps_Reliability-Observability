"""AUDIT-9.md (round 4: checker correctness, performance and security) findings as failing tests; each is
xfail(strict=True) until its sub-goal fixes it. Timed tests fail by 7x or more on the audit box."""
import time
import tracemalloc

import pytest

from app.policy import counterexample
from app.policy.checker import Monitor, check_verdicts, compile_policy, first_violations, work
from app.policy.dsl import parse
from app.policy.events import Event
from app.policy.reference import evaluate
from tests.api.test_event_ingest import ev
from tests.api.test_trace_policies import _create

US_PER_WORK = 5e-6  # `checker.work` documents about half a microsecond per unit; 10x that


def _compiled(source: str, pid: str = "p"):
    (p,) = parse(source)
    return compile_policy(pid, p)


def _many_globs(j: int, n: int = 2400) -> str:
    """One atom with n short globs (19.6 KB, 2 monitor nodes): within the 20 000-character and 2 000-node caps."""
    return f"policy g{j} severity high:\n  always not tool(" + ",".join(f'"{j}{i:x}*"' for i in range(n)) + ")"


STARS = 'policy stars severity high:\n  always not tool("' + "*" * 19_900 + 'z")'  # 2 monitor nodes


# A9-1: `checker.work` counts monitor nodes, not globs, so the 413 policy_work_too_large bound of SG-A7.3 does not see
# a glob list or a long glob; three 19.6 KB policies make each event cost 200+ ms (the glob cache thrashes)
@pytest.mark.xfail(strict=True, reason="A9-1")
async def test_check_work_bound_sees_globs(client, db, org, agent, auth_headers, api_key_headers):
    for j in range(3):
        await _create(client, auth_headers, _many_globs(j), action="block")
    body = {"agent_id": agent.id, "candidate": ev(0, "x"), "prefix": [ev(i, "y" * 255) for i in range(20)]}
    t = time.perf_counter()
    r = await client.post("/api/trace-policies/check", headers=api_key_headers, json=body)
    took = time.perf_counter() - t
    assert r.status_code in (200, 413), r.text
    assert r.status_code == 413 or took < 0.5, took


@pytest.mark.xfail(strict=True, reason="A9-1")
@pytest.mark.parametrize("sources, events", [
    ([_many_globs(j) for j in range(3)], 20),
    ([STARS], 300),
], ids=["glob-list", "long-glob"])
def test_work_estimate_bounds_monitor_time(sources, events):
    ps = [_compiled(s, str(j)) for j, s in enumerate(sources)]
    m = Monitor(ps)
    t = time.perf_counter()
    for i in range(events):
        m.step(Event(i, "tool", f"{i:03d}" + "y" * 252, "success", {}))
    assert time.perf_counter() - t < max(0.2, work(ps, events) * US_PER_WORK)


@pytest.mark.xfail(strict=True, reason="A9-1")
def test_batch_work_sees_globs():
    ps = [_compiled(_many_globs(j), str(j)) for j in range(3)]
    events = [Event(i, "tool", f"n{i}", "success", {}) for i in range(20)]
    t = time.perf_counter()
    assert check_verdicts(ps, events) == []
    assert time.perf_counter() - t < max(0.2, work(ps, len(events)) * US_PER_WORK)


# A9-2: the glob product automaton (`_automaton`, `name_patterns`) and per-glob atom terms are built before the
# counterexample deadline is checked or charged; one event, one glob, 3.7 s against a 0.2 s budget
@pytest.mark.xfail(strict=True, reason="A9-2")
@pytest.mark.parametrize("source, events", [
    ('policy u severity high:\n  always not tool("' + "".join(chr(0x4E00 + i) for i in range(4000)) + '*")',
     [Event(0, "tool", "".join(chr(0x4E00 + i) for i in range(4000)), "success", {})]),
    (_many_globs(0, 2000), [Event(i, "tool", f"n{i}", "success", {}) for i in range(99)]
     + [Event(99, "tool", "01abc", "success", {})]),
], ids=["wide-alphabet", "glob-list"])
def test_counterexample_budget_holds_for_globs(monkeypatch, source, events):
    monkeypatch.setattr(counterexample, "BUDGET_S", 0.2)
    counterexample.name_patterns.cache_clear()
    counterexample.pattern_sizes.cache_clear()
    p = _compiled(source)
    t = time.perf_counter()
    counterexample.bounded_counterexample(p, events, events[-1].seq)
    assert time.perf_counter() - t < 0.6


# A9-3: SG-A7.3's per-monitor bound on exact `count ... by` values is shared by every policy of the monitor, so
# broad count policies use up the slots and a narrow blocking policy becomes uncertain, hence false from then on:
# a legitimate call is blocked, and so is every later one in the trace
@pytest.mark.xfail(strict=True, reason="A9-3")
def test_one_policys_groups_do_not_make_another_false():
    broad = [_compiled(f"policy names{i} severity low:\n  always count(tool(*)) by name <= 50", f"b{i}")
             for i in range(20)]
    target = _compiled('policy one_delete_each severity high:\n  always count(tool("delete_*")) by name <= 1', "t")
    events = [Event(i, "tool", f"step_{i}", "success", {}) for i in range(600)]
    events += [Event(600, "tool", "delete_a", "success", {}), Event(601, "tool", "delete_b", "success", {})]
    m = Monitor(broad + [target])
    for e in events:
        m.step(e)
    assert evaluate(target.policy, events) == []
    assert "t" not in m.failing


# A9-4: the batch evaluator keeps an exact count per `count ... by` node per distinct value, each value a fresh digest
# string per node; at the org cap (about 2 000 such nodes) a 10 000-event trace needs about 2 GB
@pytest.mark.xfail(strict=True, reason="A9-4")
def test_batch_count_by_memory_is_bounded():
    body = " and ".join(f"count(tool(*)) by name < {i + 2}" for i in range(50))
    p = _compiled(f"policy c severity low:\n  always {body}")
    events = [Event(i, "tool", f"{i:040d}", "success", {}) for i in range(5000)]
    tracemalloc.start()
    try:
        assert first_violations([p], events) == [None]
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 50 * 5000 * 16, peak  # 16 bytes per node x value

