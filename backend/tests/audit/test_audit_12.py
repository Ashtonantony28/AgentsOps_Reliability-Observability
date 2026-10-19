"""AUDIT-12.md (API, SDK and tests, round 5) findings as failing tests; each is xfail(strict=True) until its sub-goal
fixes it. They run against the real app on SQLite and, in CI, on Postgres."""
import asyncio
import threading
import time

import pytest

from app.services import trace_policy_service as tps
from tests.api.test_event_ingest import _policy, ev
from tests.audit.test_audit_7 import _many_atoms

BIG = _many_atoms(500)  # 1 001 cost units: a quarter of the org cap; 199 events per checkpoint
N = 2_000


# A12-1: a long prefix saves its checkpoint only once all of it is stepped, so a check that arrives while an earlier
# one of the same history is still running (the SDK gave up on it after check_timeout_s and made its next guarded
# call) steps the whole prefix again; at the org caps every check of a trace past ~1 000 events overlaps the last
async def test_check_resumes_from_an_earlier_check_still_running(client, db, org, agent, api_key_headers,
                                                                 monkeypatch):
    await _policy(db, org, BIG, action="block")
    real, stepped, first = tps._step_inputs, {}, []
    paused, release = threading.Event(), threading.Event()

    def step(m, events):
        first or first.append(m)
        if m is first[0] and stepped.get(id(m), 0) >= N // 2:  # the first check is half done, and still running
            paused.set()
            release.wait(10)  # a fix that makes the second check wait for this one still ends
        stepped[id(m)] = stepped.get(id(m), 0) + len(events)
        return real(m, events)

    monkeypatch.setattr(tps, "_step_inputs", step)
    body = {"agent_id": agent.id, "candidate": ev(0, "n0"), "prefix": [ev(i, f"t{i % 7}") for i in range(N)]}
    one = asyncio.create_task(client.post("/api/trace-policies/check", headers=api_key_headers, json=body))
    try:
        for _ in range(3000):
            if paused.is_set() or one.done():
                break
            await asyncio.sleep(0.01)
        assert paused.is_set(), "the first check never reached half its prefix"
        r = await client.post("/api/trace-policies/check", headers=api_key_headers, json=body)
    finally:
        release.set()
    verdict = lambda b: (b["allowed"], [(v["policy_id"], v["at_seq"]) for v in b["violations"]])
    assert verdict((await one).json()) == verdict(r.json()) and r.json()["allowed"] is False  # control
    again = sum(n for k, n in stepped.items() if k != id(first[0]))
    assert again < N // 2 + tps.WORK_SLICE // 1_001, again  # stepped all N events again


async def test_overlapping_checks_of_a_growing_history_finish_in_about_one_checks_time(client, db, org, agent,
                                                                                     api_key_headers, monkeypatch):
    """A12-1 measured: 8 checks of a 4 000-event history at 3 996 units, a second apart, each one event longer (as
    an unstreamed trace sends them); they took 49 to 65 s each, when one alone took 7.9 s."""
    for i in range(4):
        await _policy(db, org, _many_atoms(499).replace("policy many", f"policy many{i}"))  # 999 units each
    post = lambda events: client.post("/api/trace-policies/check", headers=api_key_headers, json={
        "agent_id": agent.id, "candidate": ev(0, "n0"), "prefix": events})

    async def timed(events, delay=0.0):
        await asyncio.sleep(delay)
        t = time.perf_counter()
        r = await post(events)
        assert r.status_code == 200, r.text
        return time.perf_counter() - t

    monkeypatch.setattr(tps, "CHECK_DEADLINE_S", 600)  # measures overlap, not the deadline; slow runners pass 30 s
    real, stepped = tps._step_inputs, []
    monkeypatch.setattr(tps, "_step_inputs", lambda m, events: stepped.append(len(events)) or real(m, events))
    alone = await timed([ev(i, f"a{i % 7}") for i in range(4_000)])
    stepped.clear()
    history = [ev(i, f"t{i % 7}") for i in range(4_007)]
    took = await asyncio.gather(*(timed(history[:4_000 + n], n) for n in range(8)))
    print(f"one check alone {alone:.1f} s; 8 overlapping, a second apart: {' '.join(f'{t:.1f}' for t in took)} s")
    assert max(took) < 1.5 * alone + 1, (alone, took)
    assert sum(stepped) < 4_000 + 2 * tps.WORK_SLICE // 3_996, sum(stepped)  # each slice once, and the tails
