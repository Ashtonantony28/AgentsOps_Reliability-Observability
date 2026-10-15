"""AUDIT-7.md (round 3: checker correctness, performance and security) findings as failing tests; each is
xfail(strict=True) until its sub-goal fixes it. Timed tests fail by 4x or more on the audit box."""
import asyncio
import time

import pytest

from app.policy import counterexample
from app.policy.checker import Monitor, check_verdicts, compile_policy
from app.policy.dsl import parse
from app.policy.events import Event
from app.services.trace_service import OPEN_STREAMS_KEY, STREAM_IDLE_S, close_idle_streams
from tests.api.test_event_ingest import _append, _policy, ev


def _compiled(source: str):
    (p,) = parse(source)
    return compile_policy("p", p)


def _balanced(terms: list[str], op: str) -> str:
    while len(terms) > 1:
        terms = [f"({terms[i]} {op} {terms[i + 1]})" if i + 1 < len(terms) else terms[i]
                 for i in range(0, len(terms), 2)]
    return terms[0]


def _many_atoms(k: int) -> str:
    """A policy with k distinct atoms, about 18 bytes each: within the 20 000-character and 2 000-node caps."""
    return "policy many severity high:\n  always historically not " + _balanced([f'tool("n{i}")' for i in range(k)], "or")


def _trace(n: int) -> list[Event]:
    return [Event(i, "tool", "zz", "success", {}) for i in range(n - 1)] + [Event(n - 1, "tool", "n0", "success", {})]


BACKTRACKING = 'policy redos severity high:\n  always not tool("' + "*a" * 7 + 'b")'


# A7-1: globs compile to backtracking regexes (`.*a.*a...b`); 7 stars against 50 characters take over a second, and
# 10 stars against a 255-character name never finish, on the API event loop
def test_glob_matching_is_not_exponential():
    m = Monitor([_compiled(BACKTRACKING)])
    t = time.perf_counter()
    assert m.step(Event(0, "tool", "a" * 50, "success", {})) == []
    assert time.perf_counter() - t < 0.1


async def test_check_with_a_backtracking_glob_returns_quickly(client, db, org, agent, api_key_headers):
    await _policy(db, org, BACKTRACKING, action="block")
    t = time.perf_counter()
    r = await client.post("/api/trace-policies/check", headers=api_key_headers,
                          json={"agent_id": agent.id, "candidate": {"seq": 0, "name": "a" * 50}})
    assert r.status_code == 200 and r.json()["allowed"]
    assert time.perf_counter() - t < 0.3


# A7-2: the batch verdict solve is superlinear in distinct atoms x events and has no time limit
def test_batch_verdicts_scale_with_atoms_times_events():
    p, events = _compiled(_many_atoms(100)), _trace(1000)
    t = time.perf_counter()
    (v,) = check_verdicts([p], events)
    assert v.at_seq == 999
    assert time.perf_counter() - t < 0.5


# A7-2: atom terms are built and parsed before the counterexample deadline is ever checked
def test_counterexample_budget_holds_for_many_atoms(monkeypatch):
    monkeypatch.setattr(counterexample, "BUDGET_S", 0.2)
    p, events = _compiled(_many_atoms(150)), _trace(1000)
    t = time.perf_counter()
    counterexample.bounded_counterexample(p, events, 999)
    assert time.perf_counter() - t < 1.0


# A7-3: /check (and the append and whole-trace ingest paths) step the monitor on the event loop, with no bound on
# policy size x events per request, so one request stalls every other request on that worker
async def test_check_does_not_stall_the_event_loop(client, db, org, agent, api_key_headers):
    await _policy(db, org, _many_atoms(500), action="block")
    body = {"agent_id": agent.id, "candidate": {"seq": 0, "name": "x"},
            "prefix": [{"seq": i, "name": f"t{i % 50}"} for i in range(3_000)]}
    stall, done = 0.0, asyncio.Event()

    async def heartbeat():
        nonlocal stall
        while not done.is_set():
            t = time.perf_counter()
            await asyncio.sleep(0.01)
            stall = max(stall, time.perf_counter() - t)

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)
    t = time.perf_counter()
    r = await client.post("/api/trace-policies/check", headers=api_key_headers, json=body)
    took = time.perf_counter() - t
    done.set()
    await beat
    assert r.status_code == 200
    assert stall < max(0.5, took / 4), (stall, took)  # relative: a busy runner slows the whole request (A10-8)


# A7-4: a `count ... by name` node keeps up to 1 000 names of up to 255 characters each, per node, in every
# trace's Redis snapshot; a 13 KB policy with 300 such nodes makes it 73 MB
def test_monitor_snapshot_size_is_bounded():
    source = "policy wide severity high:\n  always " + _balanced(
        [f'count(not tool("z{i}")) by name < 99' for i in range(300)], "and")
    m = Monitor([_compiled(source)])
    for i in range(1000):
        m.step(Event(i, "tool", f"{i:04d}" * 63, "success", {}))
    assert len(m.snapshot()) < 8 * 2**20


# A7-5: the idle sweeper closes a trace whose agent is only paused (a human approval, a long tool call); the next
# append is a 409 trace_closed, so the SDK stops streaming and the rest of the trace is never stored or counted
async def test_append_after_an_idle_close_is_accepted(client, db, org, agent, api_key_headers, redis, enqueued):
    assert (await _append(client, api_key_headers, agent, "paused", [ev(0, "search")])).status_code == 202
    (trace_id,) = await redis.zrange(OPEN_STREAMS_KEY, 0, -1)
    assert await close_idle_streams(db, now=time.time() + STREAM_IDLE_S + 1) == [trace_id]
    r = await _append(client, api_key_headers, agent, "paused", [ev(1, "search")])
    assert r.status_code == 202, r.json()
