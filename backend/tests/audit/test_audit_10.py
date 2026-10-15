"""AUDIT-10.md (API, SDK and tests, round 4) findings as failing tests; each is xfail(strict=True) until its sub-goal
fixes it. They run against the real app (the async SDK through ASGITransport) on SQLite and, in CI, on Postgres."""
import sys
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "sdk"))

from agentops_sdk.async_client import AsyncAgentOpsClient  # noqa: E402
from app.models import Span, Trace  # noqa: E402
from app.services import trace_policy_service as tps  # noqa: E402
from tests.api.test_event_ingest import _policy, ev  # noqa: E402
from tests.audit.test_audit_7 import _many_atoms  # noqa: E402

BIG = _many_atoms(500)  # 1 001 monitor nodes: a quarter of what an org may have (MAX_ORG_POLICY_NODES)
N = 2_000  # events: 2 000 x 1 001 nodes is just over MAX_REQUEST_WORK


@pytest.fixture
def sdk_client(app_, api_key, agent, monkeypatch):
    real, transport = httpx.AsyncClient, httpx.ASGITransport(app=app_)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
    return AsyncAgentOpsClient(api_key=api_key, agent_id=agent.id, base_url="http://test")


async def _accepted_policy(client, auth_headers, action="block"):
    """Created through the API, so the org's policy caps accept it."""
    r = await client.post("/api/trace-policies", headers=auth_headers, json={"source": BIG, "action": action})
    assert r.status_code == 200, r.status_code


async def _stored_events(db, trace_uid) -> int | None:
    db.expire_all()
    tr = (await db.execute(select(Trace).where(Trace.trace_uid == trace_uid))).scalar_one_or_none()
    return None if tr is None else (await db.execute(
        select(func.count()).select_from(Span).where(Span.trace_id == tr.id))).scalar_one()


# A10-1: an unstreamed trace sends its whole history as the check's prefix; past MAX_REQUEST_WORK / nodes events
# /check answers 413, which the SDK blocks on whatever fail_closed, though no policy is violated
async def test_long_unstreamed_trace_checks_are_answered(client, auth_headers, sdk_client):
    await _accepted_policy(client, auth_headers, action="alert")
    async with sdk_client.trace() as short:
        for _ in range(N // 2):
            short.add_event("tool", "search")
        assert (await short.acheck("search")).allowed  # control: half as long is answered
    async with sdk_client.trace() as t:
        for _ in range(N):
            t.add_event("tool", "search")
        result = await t.acheck("search")
    assert not sdk_client.fail_closed and result.allowed, result.error


# A10-1: the same trace's upload is refused (only block policies count there) and the SDK drops it for good
async def test_long_unstreamed_trace_is_stored(db, client, auth_headers, sdk_client):
    await _accepted_policy(client, auth_headers)
    async with sdk_client.trace() as short:
        for _ in range(N // 2):
            short.add_event("tool", "search")
    async with sdk_client.trace() as t:
        for _ in range(N):
            t.add_event("tool", "search")
    await sdk_client.close()
    assert await _stored_events(db, short.trace_id) == N // 2  # control
    assert await _stored_events(db, t.trace_id) == N


# A10-1: a streamed trace whose pending events pile up (a slow or unreachable server) pushes them as one append; past
# the bound that is a fatal 413, the stream stops and the rest of the trace is never stored
async def test_streamed_backlog_is_delivered(db, client, auth_headers, sdk_client):
    await _accepted_policy(client, auth_headers)
    async with sdk_client.trace(stream=True) as t:
        t.add_event("tool", "search")
        await t._apush()
        assert t._sent == 1  # control: streaming
        for _ in range(N):
            t.add_event("tool", "search")
    assert not t._dead
    assert await _stored_events(db, t.trace_id) == N + 1


# A10-2: /ingest/bulk bounds each trace's work, not the request's: many traces each under MAX_REQUEST_WORK add up
async def test_bulk_upload_work_is_bounded_per_request(client, db, org, agent, api_key_headers, monkeypatch):
    monkeypatch.setattr(tps, "MAX_REQUEST_WORK", 1_000)
    await _policy(db, org, 'policy p severity high:\n  always not tool("x")', action="block")  # 2 nodes
    trace = {"agent_id": agent.id, "input": "q", "output": "a", "events": [ev(i, "t") for i in range(400)]}
    one = await client.post("/api/traces/ingest/bulk", headers=api_key_headers, json={"traces": [trace]})
    assert one.json()["rejected"] == []  # control: 800 steps of work is within the (patched) bound
    r = await client.post("/api/traces/ingest/bulk", headers=api_key_headers, json={"traces": [trace] * 3})
    rejected = r.json().get("rejected", []) if r.status_code < 300 else None
    assert r.status_code == 413 or any(x["reason"] == "policy_work_too_large" for x in rejected), r.text
