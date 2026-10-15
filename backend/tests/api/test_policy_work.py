"""SG-A7.3, SG-A10.1/A10-2: policy work runs off the event loop, long jobs in slices; an append's and a request's
uploads' work is bounded (413 / rejected policy_work_too_large), a check always answered; an org's policies are
bounded in number and size (422 policy_limit)."""
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


async def test_check_over_the_work_bound_is_answered(client, db, org, agent, api_key_headers):
    await _policy(db, org, BIG, action="block")
    for n in (1_999, 4_000):
        r = await client.post("/api/trace-policies/check", headers=api_key_headers, json={
            "agent_id": agent.id, "candidate": ev(0, "n0"), "prefix": [ev(i, "t") for i in range(n)]})
        assert r.status_code == 200 and r.json()["allowed"] is False, r.text


async def test_long_prefix_resumes_from_an_earlier_checks_checkpoint(client, db, org, agent, api_key_headers,
                                                                     monkeypatch):
    """SG-A10.1: an unstreamed trace sends its whole history with each check; the next check steps only the events
    after the last checkpoint, and a changed event or policy set starts over."""
    await _policy(db, org, BIG)
    stepped = []
    real = tps._step_inputs
    monkeypatch.setattr(tps, "_step_inputs", lambda m, es: stepped.append(len(es)) or real(m, es))
    k = tps.WORK_SLICE // 1_001  # 199 events per checkpoint
    prefix = [ev(i, f"t{i % 7}") for i in range(1_000)]

    async def check(events):
        stepped.clear()
        r = await client.post("/api/trace-policies/check", headers=api_key_headers, json={
            "agent_id": agent.id, "candidate": ev(0, "n0"), "prefix": events})
        assert r.status_code == 200, r.text
        return r.json(), sum(stepped)

    first, n = await check(prefix)
    assert n == 1_000
    again, n = await check(prefix + [ev(1_000, "t1")])
    assert n == 1_001 - 1_000 // k * k and again["allowed"] == first["allowed"]
    _, n = await check([ev(0, "other")] + prefix[1:])  # same length, another history
    assert n == 1_000
    await _policy(db, org, 'policy p severity high:\n  always not tool("x")')
    _, n = await check(prefix)
    assert n == 1_000


async def test_long_prefix_is_checked_without_redis(client, db, org, agent, api_key_headers, redis, monkeypatch):
    await _policy(db, org, BIG, action="block")

    async def down(*a, **kw):
        raise ConnectionError("redis down")
    monkeypatch.setattr(redis, "mget", down)
    r = await client.post("/api/trace-policies/check", headers=api_key_headers, json={
        "agent_id": agent.id, "candidate": ev(0, "n0"), "prefix": [ev(i, "t") for i in range(500)]})
    assert r.status_code == 200 and r.json()["allowed"] is False, r.text


async def test_checkpointed_check_agrees_with_a_fresh_one(client, db, org, agent, api_key_headers, monkeypatch):
    monkeypatch.setattr(tps, "WORK_SLICE", 50)
    await _policy(db, org, 'policy p severity high:\n  always not (tool("b") and once tool("a"))', action="block")
    body = lambda n: {"agent_id": agent.id, "candidate": ev(0, "b"), "prefix": [ev(i, "a" if i == 30 else "t")
                                                                                 for i in range(n)]}
    for n in (20, 29, 40, 31, 60):  # resumed from checkpoints before and after the "a"
        r = await client.post("/api/trace-policies/check", headers=api_key_headers, json=body(n))
        assert r.json()["allowed"] is (n <= 30), (n, r.text)


async def test_append_over_the_work_bound_is_413_with_the_events_that_fit(client, db, org, agent, api_key_headers):
    await _policy(db, org, BIG)
    r = await _append(client, api_key_headers, agent, "w1", [ev(i, "t") for i in range(2_000)])
    details = _err(r, 413, "policy_work_too_large")["details"]
    assert (details["work"], details["max_work"], details["max_events"]) == (2_002_000, tps.MAX_REQUEST_WORK, 1_998)
    assert (await db.execute(select(Trace))).scalars().all() == []
    assert (await _append(client, api_key_headers, agent, "w1", [ev(i, "t") for i in range(1_998)])).status_code == 202


async def test_single_event_append_is_never_over_the_bound(client, db, org, agent, api_key_headers, monkeypatch):
    monkeypatch.setattr(tps, "MAX_REQUEST_WORK", 1)
    await _policy(db, org, BIG)
    assert (await _append(client, api_key_headers, agent, "w1", [ev(0, "t")])).status_code == 202
    assert _err(await _append(client, api_key_headers, agent, "w1", [ev(1, "t"), ev(2, "t")]), 413,
                "policy_work_too_large")["details"]["max_events"] == 1


async def test_upload_over_the_work_bound_is_stored_and_batch_checked(client, db, org, agent, api_key_headers):
    p = await _policy(db, org, BIG, action="block")
    body = {"agent_id": agent.id, "input": "q", "output": "a", "events": [ev(i, "n0") for i in range(1_997)]}
    assert (await client.post("/api/traces/ingest", headers=api_key_headers, json=body)).status_code == 403
    body["events"] += [ev(1_997, "t"), ev(1_998, "t")]  # over the bound alone: stored, its batch check judges it
    r = await client.post("/api/traces/ingest", headers=api_key_headers, json=body)
    assert r.status_code == 202
    (row,) = await tps.check_completed_trace(db, r.json()["id"], counterexamples=False)
    assert (row.policy_id, row.at_seq) == (p.id, 0)


async def test_bulk_upload_charges_its_traces_to_one_bound(client, db, org, agent, api_key_headers, monkeypatch):
    monkeypatch.setattr(tps, "MAX_REQUEST_WORK", 1_000)
    await _policy(db, org, 'policy p severity high:\n  always not tool("x")', action="block")  # 2 nodes
    t = lambda n: {"agent_id": agent.id, "input": "q", "output": "a", "events": [ev(i, "t") for i in range(n)]}
    await _policy(db, org, 'policy q severity high:\n  always not tool("y")')  # not a block policy: not charged
    r = await client.post("/api/traces/ingest/bulk", headers=api_key_headers,
                          json={"traces": [t(300), t(600), t(200), t(100), t(501)]})
    # 600 steps, 400 left; over the bound alone (stored, its batch check judges it); 400, 0 left; refused; alone
    assert r.json()["rejected"] == [{"index": 3, "reason": "policy_work_too_large", "work": 200, "max_work": 0}]
    assert len(r.json()["accepted"]) == 4


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
    """SG-A9.1 (A9-1): a 2 400-glob atom is 2 401 units, not 2 nodes: a second one is over the org cap, and an
    append over MAX_REQUEST_WORK / 2 401 events gets a 413."""
    await _create(client, auth_headers, _many_globs(0), action="block")
    body = _err(await client.post(URL, json={"source": _many_globs(1)}, headers=auth_headers), 422, "policy_limit")
    assert (body["details"]["nodes"], body["details"]["max_nodes"]) == (4_802, tps.MAX_ORG_POLICY_NODES)
    r = await _append(client, api_key_headers, agent, "g", [ev(i, "t") for i in range(833)])
    assert _err(r, 413, "policy_work_too_large")["details"]["work"] == 833 * 2_401
    assert (await _append(client, api_key_headers, agent, "g", [ev(i, "t") for i in range(832)])).status_code == 202
