"""AUDIT-14.md (API, SDK and tests, round 6) findings as failing tests; each is xfail(strict=True) until its sub-goal
fixes it. They run against the real app on SQLite and, in CI, on Postgres; the last one through a real server."""
import asyncio
import socket
import sys
import threading
from pathlib import Path

import pytest
import uvicorn
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "sdk"))

from agentops_sdk.async_client import AsyncAgentOpsClient  # noqa: E402
from agentops_sdk.client import AgentOpsClient  # noqa: E402
from app.services import trace_policy_service as tps  # noqa: E402
from tests.api.test_event_ingest import _append, _policy, ev  # noqa: E402
from tests.api.test_trace_policies import _err  # noqa: E402
from tests.audit.test_audit_7 import _many_atoms  # noqa: E402

CHECK = "/api/trace-policies/check"
BIG = _many_atoms(500)  # 1 001 cost units: a quarter of the org cap
N = 2_000
K = tps.WORK_SLICE // 1_003  # events per slice with BIG and one 2-unit policy


async def _stale_streamed_trace(client, db, org, agent, headers):
    """A streamed trace of N stored events whose monitor state no longer fits the agent's policies (one was added),
    so its next check rebuilds it from spans; ordinary use, no Redis loss needed."""
    await _policy(db, org, BIG, action="block")
    for s in range(0, N, 500):
        r = await _append(client, headers, agent, "s1", [ev(i, f"t{i % 7}") for i in range(s, s + 500)])
        assert r.status_code == 202, r.text
    await _policy(db, org, 'policy small severity low:\n  always not tool("zz")')


def _count(monkeypatch, *names) -> dict:
    """Events stepped per monitor through the rebuild (`_steps`) and prefix (`_step_inputs`) paths."""
    stepped = {}
    for name in names:
        real = getattr(tps, name)
        monkeypatch.setattr(tps, name, lambda m, es, real=real: stepped.__setitem__(
            id(m), stepped.get(id(m), 0) + len(es)) or real(m, es))
    return stepped


# A14-1: SG-A12.1 made a long /check prefix resume from what an earlier, still running or abandoned, check stepped;
# the stored-trace half of a check is a span rebuild that a later check waits 5 s for (the rebuild lock's
# blocking_timeout) and then does again from scratch
async def test_check_of_a_stored_trace_resumes_from_a_rebuild_still_running(client, db, org, agent, api_key_headers,
                                                                            monkeypatch):
    await _stale_streamed_trace(client, db, org, agent, api_key_headers)
    real, stepped, first = tps._steps, {}, []
    paused, release = threading.Event(), threading.Event()

    def step(m, events):
        first or first.append(m)
        if m is first[0] and stepped.get(id(m), 0) >= N // 2 and not paused.is_set():  # its rebuild is half done
            paused.set()
            release.wait(7)  # past the old 5 s lock wait; a fix that makes the second check wait still ends
        stepped[id(m)] = stepped.get(id(m), 0) + len(events)
        return real(m, events)

    monkeypatch.setattr(tps, "_steps", step)
    body = {"agent_id": agent.id, "trace_uid": "s1", "candidate": ev(0, "n0")}
    one = asyncio.create_task(client.post(CHECK, headers=api_key_headers, json=body))
    try:
        for _ in range(3000):
            if paused.is_set() or one.done():
                break
            await asyncio.sleep(0.01)
        assert paused.is_set(), "the first check never reached half its rebuild"
        r = await client.post(CHECK, headers=api_key_headers, json=body)
    finally:
        release.set()
    verdict = lambda b: (b["allowed"], [(v["policy_id"], v["at_seq"]) for v in b["violations"]])
    assert verdict((await one).json()) == verdict(r.json()) and r.json()["allowed"] is False  # control
    again = sum(n for k, n in stepped.items() if k != id(first[0]))
    assert again < N // 2 + K, again  # rebuilt all N stored events again


# A14-1: a check stops between slices once its request is gone (503 check_timeout), but not while it rebuilds the
# stored trace: that runs to the end for a client that left, past CHECK_DEADLINE_S too
async def test_a_checks_rebuild_stops_once_its_request_is_gone(client, db, org, agent, api_key_headers, monkeypatch):
    await _stale_streamed_trace(client, db, org, agent, api_key_headers)
    stepped = _count(monkeypatch, "_steps", "_step_inputs")

    async def gone(self):
        return sum(stepped.values()) >= 3 * K

    monkeypatch.setattr(Request, "is_disconnected", gone)
    _err(await client.post(CHECK, headers=api_key_headers, json={
        "agent_id": agent.id, "candidate": ev(0, "n0"), "prefix": [ev(i, f"t{i % 7}") for i in range(N)]}),
        503, "check_timeout")
    assert sum(stepped.values()) == 3 * K  # control: a long prefix stops once its request is gone
    stepped.clear()
    r = await client.post(CHECK, headers=api_key_headers, json={"agent_id": agent.id, "trace_uid": "s1",
                                                                "candidate": ev(0, "n0")})
    assert r.status_code == 503 and sum(stepped.values()) < N, (r.status_code, sum(stepped.values()))


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def server(app_):
    srv = uvicorn.Server(uvicorn.Config(app_, host="127.0.0.1", port=_port(), log_level="warning", lifespan="off"))
    task = asyncio.create_task(srv.serve())
    while not srv.started:
        await asyncio.sleep(0.01)
    yield f"http://127.0.0.1:{srv.config.port}"
    srv.should_exit = True
    await task


@pytest.mark.parametrize("sync", [True, False])
async def test_abandoned_checks_stop_and_the_next_resume_through_a_real_server(server, db, org, agent, api_key,
                                                                               monkeypatch, sync):
    """SG-A12.1 through uvicorn and both SDK clients (its tests patch `Request.is_disconnected`): a check the SDK gives
    up on at check_timeout_s stops soon after, follows fail_closed, and each next check resumes until one answers."""
    await _policy(db, org, BIG, action="block")
    stepped = _count(monkeypatch, "_step_inputs")
    events = 3_000
    c = (AgentOpsClient if sync else AsyncAgentOpsClient)(api_key=api_key, agent_id=agent.id, base_url=server,
                                                          check_timeout_s=0.5, fail_closed=sync)
    t = c.trace()
    for i in range(events):
        t.add_event("tool", f"t{i % 7}")
    check = (lambda: asyncio.to_thread(t.check, "n0")) if sync else (lambda: t.acheck("n0"))
    r = await check()
    assert r.error == "timeout" and r.allowed is not sync, r
    await asyncio.sleep(1)
    first = sum(stepped.values())
    await asyncio.sleep(1)
    assert sum(stepped.values()) == first < events, first  # stopped, not stepped on to the end
    for _ in range(60):
        if (r := await check()).error is None:
            break
    assert r.allowed is False and r.violations, r
    assert sum(stepped.values()) < events + 15 * K, sum(stepped.values())  # about each slice once
    if sync:
        c.close()
    else:
        await c.close()
