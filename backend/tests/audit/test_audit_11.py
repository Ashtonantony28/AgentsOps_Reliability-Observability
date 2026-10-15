"""AUDIT-11.md (round 5: checker correctness, performance and security) findings as failing tests; each is
xfail(strict=True) until its sub-goal fixes it."""
import asyncio
import time
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.middleware.auth import hash_api_key
from app.models import Agent, ApiKey, Organization
from app.policy.checker import work
from app.services import trace_policy_service as tps
from tests.api.test_event_ingest import _policy, ev
from tests.api.test_trace_policies import _err
from tests.audit.test_audit_7 import _many_atoms

CHECK = "/api/trace-policies/check"
BIG = _many_atoms(500)  # 1 001 monitor nodes, a quarter of the org cap


async def _other_org(db):
    o = Organization(name="Other Org", slug=f"other-{uuid.uuid4().hex[:8]}")
    db.add(o)
    await db.commit()
    a = Agent(org_id=o.id, name="other-agent")
    raw = f"ak_test_{uuid.uuid4().hex}"
    db.add_all([a, ApiKey(org_id=o.id, name="test", key_prefix=raw[:8], key_hash=hash_api_key(raw))])
    await db.commit()
    return o, a, {"X-API-Key": raw}


# A11-1: policy work past WORK_SLICE runs in slices on one two-thread FIFO pool per process, shared by every org, and
# nothing bounds how many such jobs one org has in flight (`/check` has no work bound since SG-A10.1; the rate limit
# is 600 requests a minute). Another org's long check, append or rebuild then waits behind a slice of each.
@pytest.mark.xfail(strict=True, reason="A11-1")
async def test_one_orgs_policy_work_does_not_queue_another_orgs(client, db, org, agent, api_key_headers,
                                                                monkeypatch):
    monkeypatch.setattr(tps, "WORK_SLICE", 20_000)
    other, other_agent, other_headers = await _other_org(db)
    await _policy(db, org, _many_atoms(50))  # 101 nodes: 198 events per slice
    await _policy(db, other, _many_atoms(50))
    slices: list[str] = []
    real = tps._step_inputs
    monkeypatch.setattr(tps, "_step_inputs", lambda m, es: slices.extend(es[:1] and [es[0].name[0]]) or real(m, es))

    async def check(headers, agent_id, tag, n):
        prefix = [ev(i, f"{tag}{uuid.uuid4().hex}") for i in range(n)]  # never seen, so no checkpoint
        r = await client.post(CHECK, headers=headers, json={"agent_id": agent_id, "candidate": ev(0, "x"),
                                                            "prefix": prefix})
        assert r.status_code == 200, r.text

    noisy = [asyncio.create_task(check(other_headers, other_agent.id, "A", 20_000 // 101 * 40)) for _ in range(24)]
    deadline = time.monotonic() + 60
    while slices.count("A") < 48 and time.monotonic() < deadline:  # every noisy check is in its slices
        await asyncio.sleep(0.01)
    before = len(slices)
    await check(api_key_headers, agent.id, "B", 198 * 10)  # ten slices
    during = slices[before:]
    for t in noisy:
        t.cancel()
    await asyncio.gather(*noisy, return_exceptions=True)
    assert during.count("B") >= 10
    # fair or per-org bounded: at most a few of the other org's slices per slice of this one; about 40 a slice today
    assert during.count("A") <= 4 * during.count("B"), (during.count("A"), during.count("B"))


# A11-2: whole-trace uploads queue their batch check on the one `policy` Celery queue, first come first served, and
# nothing bounds the batch work an org has queued: an upload over MAX_REQUEST_WORK does no inline work at all (SG-A10.1)
# and log/alert policies never do. At the org cap a 10 000-event trace is ~31 s of batch verdicts, so one org's
# minute of uploads at its rate limit queues hours of work ahead of every other org's checks.
@pytest.mark.xfail(strict=True, reason="A11-2")
async def test_one_orgs_queued_batch_work_is_bounded(client, db, org, agent, api_key_headers, enqueued):
    other, other_agent, other_headers = await _other_org(db)
    await _policy(db, other, BIG)
    await _policy(db, org, BIG)
    n = tps.MAX_REQUEST_WORK // 1_001 + 1  # just over the bound: stored, no inline work
    for j in range(20):
        r = await client.post("/api/traces/ingest", headers=other_headers, json={
            "agent_id": other_agent.id, "events": [ev(i, f"{j}-{i}") for i in range(n)]})
        assert r.status_code in (202, 429), r.text
    r = await client.post("/api/traces/ingest", headers=api_key_headers, json={
        "agent_id": agent.id, "events": [ev(0, "a")]})
    assert r.status_code == 202, r.text
    ahead = enqueued[:enqueued.index(r.json()["id"])]
    # at most a few requests' worth of the other org's batch work ahead of this org's check; 20 today
    assert len(ahead) * work([tps._compile("x", BIG)], n) <= 4 * tps.MAX_REQUEST_WORK, len(ahead)


def _unused_tags(j: int) -> str:
    """19.7 KB of tags the formula never uses, so the policy costs 2 units (`Plan.cost`) but parses in ~20 ms."""
    tags = "".join(f'tag t{i} = tool("a{i}") or tool("b{i}")\n' for i in range(500))
    return tags + f'policy h{j} severity low:\n  always not tool("x")'


# A11-3: `active_policies` parses each policy on the event loop on a miss of the 2 048-entry `_compile` cache, which is
# shared by every org of the process; 21 self-registered orgs at the 100-policy cap make every request miss, and each
# of their requests then stalls the loop for every org for the time to parse 100 policies (2 s here)
async def test_policy_compile_does_not_stall_the_event_loop(client, db, org, agent, auth_headers, api_key_headers):
    for j in range(3):  # unused tags count toward the org cap now
        r = await client.post("/api/trace-policies", headers=auth_headers, json={"source": _unused_tags(j)})
        assert r.status_code == 200 if j < 2 else _err(r, 422, "policy_limit"), r.text
    for j in range(2, tps.MAX_ORG_POLICIES):  # stored before that
        await _policy(db, org, _unused_tags(j))
    for i in range(tps.COMPILE_CACHE_SIZE):  # the other orgs' policies, used since
        tps._compile(f"other-{i}", f'policy p{i}:\n  always not tool("x")')
    stall, done = 0.0, False

    async def heartbeat():
        nonlocal stall
        while not done:
            t = time.perf_counter()
            await asyncio.sleep(0.005)
            stall = max(stall, time.perf_counter() - t)

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.02)
    r = await client.post(CHECK, headers=api_key_headers, json={"agent_id": agent.id, "candidate": ev(0, "x")})
    done = True
    await beat
    assert r.status_code == 200, r.text
    assert stall < 0.25, stall


# A11-4: an integer of over 4 300 digits (Python's int-from-string limit) in an attr value or count bound is a
# ValueError, not a PolicyParseError: /validate and create answer 500 instead of invalid_policy
@pytest.mark.parametrize("formula", ['not attr("k", ' + "1" * 4_301 + ")", "count(tool(*)) <= " + "1" * 4_301],
                         ids=["attr", "count-bound"])
async def test_huge_integer_is_a_parse_error(app_, auth_headers, formula):
    source = f"policy p severity high:\n  always {formula}"
    async with AsyncClient(transport=ASGITransport(app=app_, raise_app_exceptions=False),
                           base_url="http://test") as client:
        r = await client.post("/api/trace-policies/validate", headers=auth_headers, json={"source": source})
        assert r.status_code == 200 and r.json()["ok"] is False, r.status_code
        r = await client.post("/api/trace-policies", headers=auth_headers, json={"source": source, "action": "log"})
        assert r.status_code == 422, r.status_code
