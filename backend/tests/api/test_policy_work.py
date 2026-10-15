"""SG-A7.3: policy work per request is bounded (413 policy_work_too_large) and runs off the event loop; an org's
policies are bounded in number and size (422 policy_limit)."""
import asyncio
import time

from sqlalchemy import select

from app.models import Trace
from app.services import trace_policy_service as tps
from tests.api.test_event_ingest import _append, _policy, ev
from tests.api.test_trace_policies import SRC_A, SRC_B, URL, _create, _err
from tests.audit.test_audit_7 import _many_atoms
from tests.audit.test_audit_9 import _many_globs

BIG = _many_atoms(500)  # 1 001 nodes: 2 000 events is over MAX_REQUEST_WORK


async def test_check_over_the_work_bound_is_413(client, db, org, agent, api_key_headers):
    await _policy(db, org, BIG)
    r = await client.post("/api/trace-policies/check", headers=api_key_headers, json={
        "agent_id": agent.id, "candidate": ev(0, "x"), "prefix": [ev(i, "t") for i in range(1_999)]})
    body = _err(r, 413, "policy_work_too_large")
    assert (body["details"]["work"], body["details"]["max_work"]) == (2_002_000, tps.MAX_REQUEST_WORK)
    r = await client.post("/api/trace-policies/check", headers=api_key_headers, json={
        "agent_id": agent.id, "candidate": ev(0, "x"), "prefix": [ev(i, "t") for i in range(1_997)]})
    assert r.status_code == 200 and r.json()["allowed"]


async def test_append_over_the_work_bound_is_413_and_stores_nothing(client, db, org, agent, api_key_headers):
    await _policy(db, org, BIG)
    r = await _append(client, api_key_headers, agent, "w1", [ev(i, "t") for i in range(2_000)])
    _err(r, 413, "policy_work_too_large")
    assert (await db.execute(select(Trace))).scalars().all() == []
    assert (await _append(client, api_key_headers, agent, "w1", [ev(i, "t") for i in range(100)])).status_code == 202


async def test_upload_counts_only_blocking_policies(client, db, org, agent, api_key_headers):
    p = await _policy(db, org, BIG)
    body = {"agent_id": agent.id, "input": "q", "output": "a", "events": [ev(i, "t") for i in range(2_000)]}
    assert (await client.post("/api/traces/ingest", headers=api_key_headers, json=body)).status_code == 202
    p.action = "block"
    await db.commit()
    _err(await client.post("/api/traces/ingest", headers=api_key_headers, json=body), 413, "policy_work_too_large")
    r = await client.post("/api/traces/ingest/bulk", headers=api_key_headers, json={"traces": [body]})
    assert r.json()["rejected"] == [{"index": 0, "reason": "policy_work_too_large", "work": 2_002_000,
                                     "max_work": tps.MAX_REQUEST_WORK}]


async def test_work_within_the_bound_runs_off_the_event_loop(client, db, org, agent, api_key_headers):
    await _policy(db, org, BIG, action="block")
    body = {"agent_id": agent.id, "candidate": ev(0, "n0"), "prefix": [ev(i, f"t{i % 50}") for i in range(1_990)]}
    stall, done = 0.0, asyncio.Event()

    async def heartbeat():
        nonlocal stall
        while not done.is_set():
            t = time.perf_counter()
            await asyncio.sleep(0.01)
            stall = max(stall, time.perf_counter() - t)

    beat = asyncio.create_task(heartbeat())
    t = time.perf_counter()
    r = await client.post("/api/trace-policies/check", headers=api_key_headers, json=body)
    took = time.perf_counter() - t
    done.set()
    await beat
    assert r.status_code == 200 and r.json()["allowed"] is False
    assert stall < max(0.2, took / 4), (stall, took)


async def test_org_policy_count_is_bounded(client, auth_headers, monkeypatch):
    monkeypatch.setattr(tps, "MAX_ORG_POLICIES", 1)
    a = await _create(client, auth_headers)
    body = _err(await client.post(URL, json={"source": SRC_B}, headers=auth_headers), 422, "policy_limit")
    assert body["details"]["max_policies"] == 1
    assert (await client.put(f"{URL}/{a['id']}", json={"source": SRC_B}, headers=auth_headers)).status_code == 200
    assert (await client.delete(f"{URL}/{a['id']}", headers=auth_headers)).status_code == 200
    await _create(client, auth_headers, SRC_A)  # archived policies do not count


async def test_org_policy_nodes_are_bounded(client, auth_headers, monkeypatch):
    monkeypatch.setattr(tps, "MAX_ORG_POLICY_NODES", 5)
    a = await _create(client, auth_headers, SRC_A)  # 2 nodes
    big = 'policy b:\n  always not (tool("x") or tool("y"))'  # 4 nodes
    body = _err(await client.post(URL, json={"source": big}, headers=auth_headers), 422, "policy_limit")
    assert (body["details"]["nodes"], body["details"]["max_nodes"]) == (6, 5)
    await _create(client, auth_headers, 'policy b:\n  always not tool("x")')
    _err(await client.put(f"{URL}/{a['id']}", json={"source": big.replace("b:", "alpha:")}, headers=auth_headers),
         422, "policy_limit")
    assert (await client.put(f"{URL}/{a['id']}", json={"source": 'policy alpha:\n  always tool(*)'},
                             headers=auth_headers)).status_code == 200


async def test_glob_lists_count_toward_work_and_the_org_cap(client, db, org, agent, auth_headers, api_key_headers):
    """SG-A9.1 (A9-1): a 2 400-glob atom is 2 401 units, not 2 nodes: a second one is over the org cap, and a
    request over MAX_REQUEST_WORK / 2 401 events gets a 413."""
    await _create(client, auth_headers, _many_globs(0), action="block")
    body = _err(await client.post(URL, json={"source": _many_globs(1)}, headers=auth_headers), 422, "policy_limit")
    assert (body["details"]["nodes"], body["details"]["max_nodes"]) == (4_802, tps.MAX_ORG_POLICY_NODES)
    r = await client.post("/api/trace-policies/check", headers=api_key_headers, json={
        "agent_id": agent.id, "candidate": ev(0, "x"), "prefix": [ev(i, "t") for i in range(832)]})
    assert _err(r, 413, "policy_work_too_large")["details"]["work"] == 833 * 2_401
    r = await client.post("/api/trace-policies/check", headers=api_key_headers, json={
        "agent_id": agent.id, "candidate": ev(0, "x"), "prefix": [ev(i, "t") for i in range(831)]})
    assert r.status_code == 200 and r.json()["allowed"]
