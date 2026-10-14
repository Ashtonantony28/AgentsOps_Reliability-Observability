"""AUDIT-6.md (API, SDK and test quality, round 2) findings as failing tests; each is xfail(strict=True) until its
sub-goal fixes it. These run on SQLite and, in CI, on Postgres; each fails on both unless its marker says otherwise."""
import asyncio
import sys
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "sdk"))

from agentops_sdk import guard  # noqa: E402
from agentops_sdk.async_client import AsyncAgentOpsClient  # noqa: E402
from app.models import (  # noqa: E402
    Agent, EvaluationRun, EvaluationSet, Organization, OrgMembership, TracePolicy, TracePolicyViolation,
)
from app.middleware.auth import make_access_token  # noqa: E402
from app.policy.dsl import parse  # noqa: E402

ONE_DELETE = 'policy one_delete severity high:\n  always count(tool("delete_*")) <= 1'
NEEDS_PRIOR = 'policy needs_prior severity low:\n  always previously event("confirmation")'


async def _policy(db, org, source, action="block"):
    (p,) = parse(source)
    row = TracePolicy(org_id=org.id, name=p.name, source=source, severity=p.severity, action=action)
    db.add(row)
    await db.commit()
    return row


@pytest.fixture
def sdk_client(app_, api_key, agent, monkeypatch):
    real, transport = httpx.AsyncClient, httpx.ASGITransport(app=app_)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
    return AsyncAgentOpsClient(api_key=api_key, agent_id=agent.id, base_url="http://test")


# A6-6: the org summary report lists other organisations' evaluation runs
async def test_summary_report_lists_only_own_evaluation_runs(client, db, auth_headers):
    other = Organization(name="Other", slug=f"other-{uuid.uuid4().hex[:8]}")
    db.add(other)
    await db.flush()
    agent = Agent(org_id=other.id, name="their-agent")
    eset = EvaluationSet(org_id=other.id, name="their-set")
    db.add_all([agent, eset])
    await db.flush()
    db.add(EvaluationRun(set_id=eset.id, agent_id=agent.id, score=0.42))
    await db.commit()
    r = await client.get("/api/reports/summary", headers=auth_headers)
    assert r.status_code == 200 and r.json()["agents"] == []  # control: the caller's org has no agents
    assert r.json()["recent_evals"] == []


# A6-7: two guarded calls running concurrently in one trace are both checked against the same prefix
async def test_concurrent_guarded_calls_respect_a_count_policy(db, org, sdk_client):
    await _policy(db, org, ONE_DELETE)
    ran = []

    @guard
    async def delete_user(uid):
        await asyncio.sleep(0.01)
        ran.append(uid)

    async with sdk_client.trace(stream=True):
        await asyncio.gather(delete_user(1), delete_user(2), return_exceptions=True)
    assert ran  # control: the policy allows one delete
    assert len(ran) == 1


# A6-8: deleting a policy deleted every violation it recorded (ON DELETE CASCADE); it now archives the policy
async def test_deleting_a_policy_keeps_its_violation_history(client, db, org, agent, api_key_headers, auth_headers):
    p = await _policy(db, org, NEEDS_PRIOR, action="log")
    r = await client.post("/api/traces/t1/events", headers=api_key_headers,
                          json={"agent_id": agent.id, "events": [{"seq": 0, "name": "a"}], "final": True})
    assert r.status_code == 202 and len(r.json()["violations"]) == 1  # control
    r = await client.delete(f"/api/trace-policies/{p.id}", headers=auth_headers)
    assert r.status_code == 200
    r = await client.get("/api/reports/violations.csv", headers=auth_headers)
    assert "needs_prior" in r.text


# A6-9: the SQLite test engine enforced no foreign keys, so the default suite could not see A6-8
async def test_test_database_enforces_foreign_keys(db, org, agent):
    db.add(TracePolicyViolation(
        org_id=org.id, trace_id="no-such-trace", agent_id=agent.id, policy_id="no-such-policy", policy_name="p",
        policy_version=1, severity="low", at_seq=0, counterexample=[], message="m", mode="live"))
    with pytest.raises(IntegrityError):
        await db.commit()


# A6-10: the create endpoint only parsed; a policy nested a few hundred deep then failed every append and check
async def test_deeply_nested_policy_does_not_break_ingest(client, agent, api_key_headers, auth_headers):
    r = await client.post("/api/trace-policies", headers=auth_headers,
                          json={"source": 'policy deep severity low:\n  always ' + 'not ' * 400 + 'tool("a")'})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_policy"
    deepest = ["not " * 63 + 'tool("b")', "(" * 62 + 'not tool("b")' + ")" * 62,
               "count(" * 62 + 'tool("b")' + ") by name > 0" * 62 + ' -> not tool("b")']
    for i, f in enumerate(deepest):
        r = await client.post("/api/trace-policies", headers=auth_headers, json={"source": f"policy d{i}:\n  always {f}"})
        assert r.status_code == 200, r.text
    r = await client.post("/api/trace-policies/check", headers=api_key_headers,
                          json={"agent_id": agent.id, "candidate": {"seq": 0, "name": "a"}})
    assert r.status_code == 200, r.text
    r = await client.post("/api/traces/t1/events", headers=api_key_headers,
                          json={"agent_id": agent.id, "events": [{"seq": 0, "name": "a"}], "final": True})
    assert r.status_code == 202, r.text


# A6-11: /ws/traces checks only the token's signature: a user removed from the org keeps the live feed
@pytest.mark.xfail(strict=True, reason="A6-11: the websocket feed skips the membership and token-type checks")
def test_ws_feed_requires_membership(app_, org, admin_user, session_maker):
    async def leave():
        async with session_maker() as s:
            await s.execute(delete(OrgMembership).where(OrgMembership.user_id == admin_user.id))
            await s.commit()
    asyncio.run(leave())
    token = make_access_token(admin_user.id, org.id, "admin")
    with TestClient(app_) as tc:
        r = tc.get("/api/trace-policies", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403  # control: REST refuses the same token
        with pytest.raises(WebSocketDisconnect), tc.websocket_connect(f"/ws/traces?token={token}"):
            pass
