"""AUDIT-8.md (API, SDK and tests, round 3) findings as failing tests; each is xfail(strict=True) until its sub-goal
fixes it. They run against the real app (the SDK through ASGITransport) on SQLite and, in CI, on Postgres."""
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select, update
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "sdk"))

from agentops_sdk import guard  # noqa: E402
from agentops_sdk.async_client import AsyncAgentOpsClient  # noqa: E402
from app.models import ApiKey, Span, Trace, TracePolicy  # noqa: E402
from app.policy.dsl import parse  # noqa: E402
from app.services import trace_service  # noqa: E402
from app.services.trace_service import STREAM_IDLE_S, close_idle_streams  # noqa: E402

NO_EXFIL = 'policy no_exfil severity critical:\n  always tool("http_post") -> not once tool("read_secret")'
ONE_DELETE = 'policy one_delete severity high:\n  always count(tool("delete_*")) <= 1'


async def _policy(db, org, source, action="block"):
    (p,) = parse(source)
    db.add(TracePolicy(org_id=org.id, name=p.name, source=source, severity=p.severity, action=action))
    await db.commit()


@pytest.fixture
def sdk_client(app_, api_key, agent, monkeypatch):
    real, transport = httpx.AsyncClient, httpx.ASGITransport(app=app_)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
    return AsyncAgentOpsClient(api_key=api_key, agent_id=agent.id, base_url="http://test")


def _after_idle() -> float:
    return time.time() + STREAM_IDLE_S + 1


# A8-1: once a stream stops, checks are judged on the server's events only; the SDK's undelivered ones are ignored
@pytest.mark.xfail(strict=True, reason="A8-1: checks of a stopped stream leave out the events the SDK still holds")
async def test_check_after_idle_close_sees_undelivered_events(db, org, sdk_client):
    await _policy(db, org, NO_EXFIL)
    async with sdk_client.trace(stream=True) as t:
        t.add_event("tool", "search")
        await t._apush()
        await close_idle_streams(db, now=_after_idle())  # the agent paused for over an hour
        t.add_event("tool", "read_secret")
        await t._apush()
        result = await t.acheck("http_post")
    assert t._dead and result.error is None  # control: the server answered the check
    assert not result.allowed


# A8-1: at the event cap /check answers 409, which the SDK reads as an outage and allows
@pytest.mark.xfail(strict=True, reason="A8-1: a check of a trace at the event cap fails open")
async def test_check_at_the_event_cap_does_not_allow(db, org, sdk_client, monkeypatch):
    monkeypatch.setattr(trace_service, "MAX_TRACE_EVENTS", 3)
    await _policy(db, org, NO_EXFIL)
    async with sdk_client.trace(stream=True) as t:
        for name in ("read_secret", "search", "search", "search"):  # the fourth is a 413
            t.add_event("tool", name)
            await t._apush()
        result = await t.acheck("http_post")
    assert t._dead and t._sent == 3  # control: the server holds the secret read
    assert not result.allowed


# A8-2: the idle sweep closes a trace that is only slow; its later events and outcome are dropped
@pytest.mark.xfail(strict=True, reason="A8-2: events and outcome sent after an hour's pause are lost")
async def test_events_after_an_idle_pause_are_stored(db, org, sdk_client):
    async with sdk_client.trace(stream=True, input="hi") as t:
        t.add_event("tool", "search")
        await t._apush()
        assert await close_idle_streams(db, now=_after_idle())  # control
        t.add_event("tool", "send_email")
        t.set_output("done")
    await sdk_client.close()
    db.expire_all()
    stored = {}
    for tr in (await db.execute(select(Trace).where(Trace.trace_uid == t.trace_id))).scalars():
        spans = (await db.execute(select(Span.name).where(Span.trace_id == tr.id).order_by(Span.seq))).scalars()
        stored[tr.output] = list(spans)
    assert stored.get("done") == ["search", "send_email"]


# A8-3: guarded calls started inside a guarded call inherit its "held" context and skip the per-trace lock
@pytest.mark.xfail(strict=True, reason="A8-3: concurrent guarded calls nested in a guarded call share one check")
async def test_concurrent_guarded_calls_inside_a_guarded_call(db, org, sdk_client):
    await _policy(db, org, ONE_DELETE)
    ran = []

    @guard
    async def delete_user(uid):
        await asyncio.sleep(0.01)
        ran.append(uid)

    @guard
    async def cleanup():
        await asyncio.gather(delete_user(1), delete_user(2), return_exceptions=True)

    async with sdk_client.trace(stream=True):
        await cleanup()
    assert ran  # control: the policy allows one delete
    assert len(ran) == 1


# A8-3: a guarded call waiting on a task that makes its own guarded call never finishes
@pytest.mark.xfail(strict=True, reason="A8-3: a guarded call that awaits another task's guarded call deadlocks")
async def test_guarded_call_awaiting_a_worker_finishes(db, org, sdk_client):
    jobs = asyncio.Queue()

    @guard
    async def fetch(x):
        return x

    async def worker():
        x, fut = await jobs.get()
        fut.set_result(await fetch(x))

    @guard
    async def plan():
        fut = asyncio.get_running_loop().create_future()
        await jobs.put((1, fut))
        return await fut

    async with sdk_client.trace(stream=True):
        w = asyncio.create_task(worker())  # started before plan(), so it does not inherit plan's hold
        call = asyncio.ensure_future(plan())
        done, _ = await asyncio.wait([call], timeout=2)
        call.cancel()
        w.cancel()
    assert jobs.empty()  # control: the worker took the job
    assert done


# A8-4: /ws/ingest checks the API key once, at connect; a revoked key keeps writing on an open socket
@pytest.mark.xfail(strict=True, reason="A8-4: an open /ws/ingest socket outlives its API key's revocation")
def test_ws_ingest_stops_after_the_key_is_revoked(app_, api_key, agent, session_maker):
    def send(ws, seq):
        ws.send_text(json.dumps({"type": "events", "trace_uid": "w1", "agent_id": agent.id,
                                 "events": [{"seq": seq, "name": "a"}]}))
        return ws.receive_json()

    async def revoke():
        async with session_maker() as s:
            await s.execute(update(ApiKey).values(revoked_at=datetime.utcnow()))
            await s.commit()

    with TestClient(app_) as tc, tc.websocket_connect(f"/ws/ingest?api_key={api_key}") as ws:
        assert send(ws, 0)["ok"] is True
        asyncio.run(revoke())
        r = tc.post("/api/traces/w1/events", headers={"X-API-Key": api_key},
                    json={"agent_id": agent.id, "events": [{"seq": 1, "name": "a"}]})
        assert r.status_code == 401  # control: REST refuses the key
        assert send(ws, 1)["ok"] is False


# A8-5: /check answers 409 for trace_too_long where the append answers 413
@pytest.mark.xfail(strict=True, reason="A8-5: /check and the append disagree on the trace_too_long status")
async def test_check_and_append_agree_on_trace_too_long(client, agent, api_key_headers, monkeypatch):
    monkeypatch.setattr(trace_service, "MAX_TRACE_EVENTS", 1)
    r = await client.post("/api/traces/u1/events", headers=api_key_headers,
                          json={"agent_id": agent.id, "events": [{"seq": 0, "name": "a"}]})
    assert r.status_code == 202
    append = await client.post("/api/traces/u1/events", headers=api_key_headers,
                               json={"agent_id": agent.id, "events": [{"seq": 1, "name": "a"}]})
    check = await client.post("/api/trace-policies/check", headers=api_key_headers,
                              json={"agent_id": agent.id, "trace_uid": "u1", "candidate": {"seq": 0, "name": "a"}})
    assert append.json()["error"]["code"] == check.json()["error"]["code"] == "trace_too_long"  # control
    assert check.status_code == append.status_code == 413


# A8-6: a streamed trace's tool calls (add_tool_call) are never streamed, and its checks send no prefix either
@pytest.mark.xfail(strict=True, reason="A8-6: checks of a streamed trace ignore its add_tool_call history")
async def test_streamed_trace_checks_see_its_tool_calls(db, org, sdk_client):
    await _policy(db, org, NO_EXFIL)
    async with sdk_client.trace() as plain:
        plain.add_tool_call("read_secret")
        assert not (await plain.acheck("http_post")).allowed  # control: the same trace, not streamed
    async with sdk_client.trace(stream=True) as t:
        t.add_tool_call("read_secret")
        result = await t.acheck("http_post")
    assert not result.allowed
