"""AUDIT-3.md (API/SDK consistency) findings as failing tests; each is xfail(strict=True) until its sub-goal fixes it."""
import sys
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "sdk"))

from agentops_sdk.async_client import AsyncAgentOpsClient  # noqa: E402
from app.models import AlertEvent, AlertRule, PolicyRule, Span, Trace, TracePolicy  # noqa: E402
from app.policy.dsl import parse  # noqa: E402
from app.schemas.policy import PolicyCheckIn  # noqa: E402
from app.services import violation_service  # noqa: E402

NO_DROP = 'policy no_drop severity critical:\n  always not tool("drop_db")'
NO_EXFIL = 'policy no_exfil severity critical:\n  always tool("send_email") -> not once tool("vault.read")'
NEEDS_PRIOR = 'policy needs_prior severity low:\n  always previously event("confirmation")'


async def _policy(db, org, source, action="block", **kw):
    (p,) = parse(source)
    row = TracePolicy(org_id=org.id, name=p.name, source=source, severity=p.severity, action=action, **kw)
    db.add(row)
    await db.commit()
    return row


async def _count(db, model, *where):
    return (await db.execute(select(func.count()).select_from(model).where(*where))).scalar_one()


async def _check(client, headers, agent, name, trace_uid=None):
    return await client.post("/api/trace-policies/check", headers=headers, json={
        "agent_id": agent.id, "trace_uid": trace_uid, "candidate": {"seq": 0, "kind": "tool", "name": name}})


@pytest.fixture
def sdk_client(app_, api_key, agent, monkeypatch):
    real, transport = httpx.AsyncClient, httpx.ASGITransport(app=app_)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
    return AsyncAgentOpsClient(api_key=api_key, agent_id=agent.id, base_url="http://test")


# A3-1: policy scope uses the SDK's environment for whole traces, the agent row's for streams and checks
@pytest.mark.xfail(strict=True, reason="A3-1: streamed traces take agent.environment, not the SDK's environment")
async def test_environment_scoped_block_policy_applies_to_streamed_trace(db, org, sdk_client):
    sdk_client.environment = "prod"
    await _policy(db, org, NO_DROP, environment="prod")
    async with sdk_client.trace() as t:
        t.add_event("tool", "drop_db")
    await sdk_client.flush()
    assert await _count(db, Trace) == 0  # whole trace: blocked

    async with sdk_client.trace(stream=True) as t:
        t.add_event("tool", "drop_db")
    assert await _count(db, Span) == 0  # streamed: should be blocked too


@pytest.mark.xfail(strict=True, reason="A3-1: precheck takes agent.environment, not the SDK's environment")
async def test_environment_scoped_block_policy_applies_to_check(db, org, sdk_client):
    sdk_client.environment = "prod"
    await _policy(db, org, NO_DROP, environment="prod")
    assert not (await sdk_client.acheck("drop_db")).allowed


# A3-2: a streamed trace never sends its trace-level fields
@pytest.mark.xfail(strict=True, reason="A3-2: streamed traces drop status, input, output, tokens and cost")
async def test_streamed_trace_keeps_trace_fields(db, sdk_client):
    with pytest.raises(RuntimeError):
        async with sdk_client.trace(stream=True, input="refund order 7") as t:
            t.add_event("tool", "search")
            t.set_output("partial", tokens_input=10, tokens_output=5, cost_usd=0.25)
            raise RuntimeError("boom")
    trace = (await db.execute(select(Trace).execution_options(populate_existing=True))).scalar_one()
    assert (trace.status, trace.input, trace.output, trace.tokens_input, trace.cost_usd) == (
        "error", "refund order 7", "partial", 10, 0.25)


@pytest.mark.xfail(strict=True, reason="A3-2: legacy forbid_pii never sees a streamed trace's input text")
async def test_streamed_trace_input_is_scanned_by_legacy_pii_rule(db, org, sdk_client):
    db.add(PolicyRule(org_id=org.id, name="no-pii", rule={"forbid_pii": True}, action="block"))
    await db.commit()
    async with sdk_client.trace(input="mail jane.doe@example.com") as t:
        t.add_event("tool", "search")
    await sdk_client.flush()
    assert await _count(db, Trace) == 0  # whole trace: blocked

    async with sdk_client.trace(stream=True, input="mail jane.doe@example.com") as t:
        t.add_event("tool", "search")
    assert await _count(db, Span) == 0


# A3-3: precheck ignores legacy PolicyRule block rules that the append path enforces
async def test_check_agrees_with_append_on_legacy_block_rule(client, db, org, agent, api_key_headers):
    db.add(PolicyRule(org_id=org.id, name="no-drop", rule={"forbid_tool": "drop_db"}, action="block"))
    await db.commit()
    r = await client.post("/api/traces/t1/events", headers=api_key_headers,
                          json={"agent_id": agent.id, "events": [{"seq": 0, "name": "drop_db"}]})
    assert r.status_code == 403
    r = await _check(client, api_key_headers, agent, "drop_db")
    assert r.status_code == 200 and r.json()["allowed"] is False


# A3-5: a check from a non-streamed trace carries none of the trace's events
@pytest.mark.xfail(strict=True, reason="A3-5: non-streamed Trace.acheck sends no history, so the guard misses it")
async def test_non_streamed_trace_check_sees_its_events(db, org, sdk_client):
    await _policy(db, org, NO_EXFIL)
    async with sdk_client.trace() as t:
        t.add_event("tool", "vault.read")
        result = await t.acheck("send_email")
    assert not result.allowed


# A3-6: checks share the ingest rate limit, and the SDK fails open on 429
async def test_check_is_not_starved_by_ingest(client, db, org, agent, api_key_headers):
    await _policy(db, org, NO_DROP)
    org.rate_limit_per_min = 2
    await db.commit()
    for _ in range(2):
        r = await client.post("/api/traces/ingest", headers=api_key_headers, json={"agent_id": agent.id})
        assert r.status_code == 202
    r = await _check(client, api_key_headers, agent, "drop_db")
    assert r.status_code == 200 and r.json()["allowed"] is False


# A3-7: the API cannot say whether an empty counterexample is pending or final
@pytest.mark.xfail(strict=True, reason="A3-7: a live row reads the same before and after its batch check")
async def test_violation_says_when_counterexample_is_final(client, db, org, agent, api_key_headers, auth_headers,
                                                           enqueued):
    await _policy(db, org, NEEDS_PRIOR, action="log")
    r = await client.post("/api/traces/t1/events", headers=api_key_headers,
                          json={"agent_id": agent.id, "events": [{"seq": 0, "name": "a"}], "final": True})
    assert r.status_code == 202 and len(r.json()["violations"]) == 1
    (trace_id,) = enqueued
    before = (await client.get(f"/api/traces/{trace_id}/violations", headers=auth_headers)).json()
    await violation_service.check_and_publish(db, trace_id)
    after = (await client.get(f"/api/traces/{trace_id}/violations", headers=auth_headers)).json()
    assert before[0]["counterexample"] == after[0]["counterexample"] == []
    assert before != after


# A3-8: `log` and `alert` actions behave the same
async def test_log_policy_does_not_fire_alert_rule(client, db, org, agent, api_key_headers):
    await _policy(db, org, NO_DROP, action="log")
    db.add(AlertRule(org_id=org.id, name="pv", metric="policy_violations", operator=">=", threshold=1,
                     window_minutes=15, severity="warning", context={}))
    await db.commit()
    r = await client.post("/api/traces/t1/events", headers=api_key_headers,
                          json={"agent_id": agent.id, "events": [{"seq": 0, "name": "drop_db"}]})
    assert r.status_code == 202 and len(r.json()["violations"]) == 1
    assert await _count(db, AlertEvent) == 0


# A3-9: whole-trace ingest is not idempotent on trace_id
async def test_resent_trace_is_stored_once(client, db, org, agent, api_key_headers):
    await _policy(db, org, NO_DROP, action="log")
    body = {"agent_id": agent.id, "trace_id": "uid-1", "events": [{"seq": 0, "name": "drop_db"}]}
    for _ in range(2):
        r = await client.post("/api/traces/ingest", headers=api_key_headers, json=body)
        assert r.status_code in (202, 409)
    assert await _count(db, Trace, Trace.trace_uid == "uid-1") == 1


# A3-11: /ws/ingest skips the rate limit the REST ingest paths enforce
def test_ws_ingest_is_rate_limited(app_, session_maker, org, agent, api_key):
    import asyncio

    async def limit():
        async with session_maker() as s:
            o = await s.get(type(org), org.id)
            o.rate_limit_per_min = 1
            await s.commit()
    asyncio.run(limit())
    with TestClient(app_) as tc, tc.websocket_connect(f"/ws/ingest?api_key={api_key}") as ws:
        replies = []
        for i in range(3):
            ws.send_json({"type": "events", "trace_uid": "w1", "agent_id": agent.id,
                          "events": [{"seq": i, "name": "search"}]})
            replies.append(ws.receive_json())
    assert [r["ok"] for r in replies] == [True, False, False]


# A3-12: error codes are not stable across paths
@pytest.mark.xfail(strict=True, reason="A3-12: unknown agent is 404 http_error on /check, 400 http_error on ingest")
async def test_unknown_agent_has_one_error_code(client, api_key_headers):
    r1 = await client.post("/api/trace-policies/check", headers=api_key_headers,
                           json={"agent_id": "nope", "candidate": {"seq": 0, "name": "a"}})
    r2 = await client.post("/api/traces/t1/events", headers=api_key_headers,
                           json={"agent_id": "nope", "events": [{"seq": 0, "name": "a"}]})
    r3 = await client.post("/api/traces/ingest", headers=api_key_headers, json={"agent_id": "nope"})
    assert {r.status_code for r in (r1, r2, r3)} == {404}
    assert {r.json()["error"]["code"] for r in (r1, r2, r3)} == {"agent_not_found"}


# A3-13: policy environment is free text
@pytest.mark.xfail(strict=True, reason="A3-13: a misspelt environment is accepted and the policy never applies")
async def test_policy_environment_is_validated(client, auth_headers):
    r = await client.post("/api/trace-policies", headers=auth_headers,
                          json={"source": NO_DROP, "action": "block", "environment": "production"})
    assert r.status_code == 422


# A3-14: the candidate's seq is required but ignored
@pytest.mark.xfail(strict=True, reason="A3-14: PolicyCheckIn requires candidate.seq, which the server ignores")
def test_check_candidate_needs_no_seq():
    PolicyCheckIn.model_validate({"agent_id": "a", "candidate": {"name": "x"}})

