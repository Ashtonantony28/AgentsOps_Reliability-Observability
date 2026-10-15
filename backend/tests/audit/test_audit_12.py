"""AUDIT-12.md (API, SDK and tests, round 5) findings as failing tests; each is xfail(strict=True) until its sub-goal
fixes it. They run against the real app on SQLite and, in CI, on Postgres."""
import asyncio
import threading

import pytest

from app.services import trace_policy_service as tps
from tests.api.test_event_ingest import _policy, ev
from tests.audit.test_audit_7 import _many_atoms

BIG = _many_atoms(500)  # 1 001 cost units: a quarter of the org cap; 199 events per checkpoint
N = 2_000


# A12-1: a long prefix saves its checkpoint only once all of it is stepped, so a check that arrives while an earlier
# one of the same history is still running (the SDK gave up on it after check_timeout_s and made its next guarded
# call) steps the whole prefix again; at the org caps every check of a trace past ~1 000 events overlaps the last
@pytest.mark.xfail(strict=True, reason="A12-1: a check overlapping an earlier one of the same prefix starts over")
async def test_check_resumes_from_an_earlier_check_still_running(client, db, org, agent, api_key_headers,
                                                                 monkeypatch):
    await _policy(db, org, BIG, action="block")
    real, stepped, first = tps._step_inputs, {}, []
    paused, release = threading.Event(), threading.Event()

    def step(m, events):
        first or first.append(m)
        if m is first[0] and stepped.get(id(m), 0) >= N // 2:  # the first check is half done, and still running
            paused.set()
            release.wait(30)
        stepped[id(m)] = stepped.get(id(m), 0) + len(events)
        return real(m, events)

    monkeypatch.setattr(tps, "_step_inputs", step)
    body = {"agent_id": agent.id, "candidate": ev(0, "n0"), "prefix": [ev(i, f"t{i % 7}") for i in range(N)]}
    one = asyncio.create_task(client.post("/api/trace-policies/check", headers=api_key_headers, json=body))
    try:
        while not paused.is_set():
            await asyncio.sleep(0.01)
        r = await client.post("/api/trace-policies/check", headers=api_key_headers, json=body)
    finally:
        release.set()
    verdict = lambda b: (b["allowed"], [(v["policy_id"], v["at_seq"]) for v in b["violations"]])
    assert verdict((await one).json()) == verdict(r.json()) and r.json()["allowed"] is False  # control
    again = sum(n for k, n in stepped.items() if k != id(first[0]))
    assert again < N // 2 + tps.WORK_SLICE // 1_001, again  # stepped all N events again
