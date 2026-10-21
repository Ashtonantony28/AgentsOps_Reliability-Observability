"""AUDIT-13.md (round 6: checker correctness, performance and security) findings as failing tests; each is
xfail(strict=True) until its sub-goal fixes it."""
import asyncio
import json
import time
import uuid

import pytest

from app.services import trace_policy_service as tps
from tests.api.test_event_ingest import _append, _policy, ev
from tests.audit.test_audit_7 import _many_atoms

CHECK = "/api/trace-policies/check"
# ten `count ... by name` nodes: once 1 000 names are seen each snapshot holds 10 000 values (~370 KB)
COUNTS = "policy c severity high:\n  always " + " and ".join(f"count(tool(*)) by name <= {10_000 + i}"
                                                         for i in range(10))


async def _long_check(client, headers, agent, prefix):
    r = await client.post(CHECK, headers=headers, json={"agent_id": agent.id, "candidate": ev(0, "x"),
                                                        "prefix": prefix})
    assert r.status_code == 200, r.text
    return r.json()


async def _checkpoints(redis) -> list[int]:
    return sorted([await redis.strlen(k) async for k in redis.scan_iter("policy:prefix:*")])


async def _setup_counts(db, org, monkeypatch):
    await _policy(db, org, COUNTS)
    monkeypatch.setattr(tps, "WORK_SLICE", tps.work([tps._compile("c", COUNTS)], 20))  # 20 events a slice
    return [ev(i, f"{i:032d}") for i in range(2_000)]


# A13-1: since SG-A12.1 every slice of a long /check stores its own checkpoint for PREFIX_TTL_S (an hour), where one
# per check was stored before: a 10 000-event history at the org cap is 188 snapshots, 70 MB of Redis (was 0.4 MB),
# and one org's 4 concurrent checks added 292 MB in two minutes against the real app (4 MB before). Redis has no
# memory cap and is also the Celery broker, the monitor state and the batch turn lists of every org.
@pytest.mark.xfail(strict=True, reason="A13-1")
async def test_a_long_check_keeps_about_one_checkpoint(client, db, org, agent, api_key_headers, redis, monkeypatch):
    prefix = await _setup_counts(db, org, monkeypatch)
    await _long_check(client, api_key_headers, agent, prefix)
    sizes = await _checkpoints(redis)
    assert sizes and sizes[-1] > 100_000  # the newest holds every value counted
    # a few snapshots' worth per history; 100 of them (75 x the newest) today
    assert sum(sizes) <= 4 * sizes[-1], (len(sizes), sum(sizes), sizes[-1])


# A13-4: before each slice `_Steps.newest` reads every checkpoint after it (`mget(keys[j:])`) to restore one, so a
# check that resumes a history pulls all of them from Redis into the API process: 70 MB and 0.4 to 1.2 s per check
# for a 10 000-event history at the org cap, for the SDK's every guarded call of an unstreamed trace
@pytest.mark.xfail(strict=True, reason="A13-4")
async def test_a_resumed_check_reads_about_one_checkpoint(client, db, org, agent, api_key_headers, redis,
                                                          monkeypatch):
    prefix = await _setup_counts(db, org, monkeypatch)
    first = await _long_check(client, api_key_headers, agent, prefix)
    newest = (await _checkpoints(redis))[-1]
    read, real = [], redis.mget

    async def mget(keys):
        out = await real(keys)
        read.append(sum(len(b or "") for b in out))
        return out

    monkeypatch.setattr(redis, "mget", mget)
    again = await _long_check(client, api_key_headers, agent, prefix + [ev(2_000, "y")])
    assert again["allowed"] == first["allowed"] and read
    # about the one checkpoint it restores; every one of them (75 x the newest) today
    assert sum(read) <= 4 * newest, (sum(read), newest)


async def _max_stall(coro) -> tuple:
    stall, done = 0.0, False

    async def heartbeat():
        nonlocal stall
        while not done:
            t = time.perf_counter()
            await asyncio.sleep(0.005)
            stall = max(stall, time.perf_counter() - t)

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.02)
    try:
        return await coro, stall
    finally:
        done = True
        await beat


# A13-2: the event loop does work for requests nobody authenticated: FastAPI parses and validates a request body (no
# size limit anywhere) before `/check` or ingest looks at the API key, and login and registration run bcrypt (0.3 s)
# on the loop. A 20 MB body stalls every org's requests 2.4 to 4 s and is then a 401.
@pytest.mark.xfail(strict=True, reason="A13-2")
@pytest.mark.parametrize("vector", ["body", "login"])
async def test_unauthenticated_requests_do_not_stall_the_event_loop(client, vector):
    if vector == "body":
        body = json.dumps({"agent_id": "x", "candidate": ev(0, "a", attrs={f"k{j}": [j] for j in range(1_000_000)})})
        send = client.post(CHECK, content=body, headers={"content-type": "application/json", "X-API-Key": "nope"})
        expected = (401, 413)
    else:
        email = f"u-{uuid.uuid4().hex[:8]}@example.com"
        r = await client.post("/api/auth/register", json={"email": email, "password": "password123"})
        assert r.status_code == 200, r.text
        send = client.post("/api/auth/login", json={"email": email, "password": "wrong-password"})
        expected = (401, 429)
    r, stall = await _max_stall(send)
    assert r.status_code in expected, r.status_code
    assert stall < 0.1, stall


# A13-3: SG-A11.1 takes pool slices, rebuilds and batch checks in turn by org, but registration is open and unbounded
# (`POST /api/auth/register`, no rate limit, no verification) and makes an org: with 24 orgs one check each, another
# org's check saw 21.5 of their slices per slice of its own (2 with one org's 24 checks; 24 with the old FIFO pool).
@pytest.mark.xfail(strict=True, reason="A13-3")
async def test_one_client_cannot_make_orgs_without_bound(client):
    codes = []
    for i in range(12):
        r = await client.post("/api/auth/register", json={
            "email": f"sybil-{i}-{uuid.uuid4().hex[:6]}@example.com", "password": "password123", "org_name": f"s{i}"})
        codes.append(r.status_code)
    assert codes[0] == 200 and set(codes) <= {200, 429}, codes
    # a handful of orgs per client and hour; 12 of 12 today
    assert codes.count(200) <= 5, codes


# A13-5: a /check of a stored trace whose monitor state is gone (Redis loss, the 24 h expiry, a closed trace)
# rebuilds it from spans under a lock it waits for only 5 s, so checks arriving meanwhile (the SDK's next guarded
# calls, once it gave up on the first after 2 s) each rebuild the whole trace again, and none of them looks at its
# deadline or whether its request is gone; at the org caps a 10 000-event rebuild is ~20 s
@pytest.mark.xfail(strict=True, reason="A13-5")
async def test_concurrent_checks_rebuild_a_stored_trace_once(client, db, org, agent, api_key_headers, redis,
                                                             monkeypatch):
    await _policy(db, org, _many_atoms(250))
    for j in range(2):
        r = await _append(client, api_key_headers, agent, "t1", [ev(j * 1_000 + i, f"e{i}") for i in range(1_000)])
        assert r.status_code == 202, r.text
    await redis.delete(*[k async for k in redis.scan_iter("policy:monitor:*")])
    lock = redis.lock
    monkeypatch.setattr(redis, "lock", lambda name, **kw: lock(name, **{**kw, "blocking_timeout": 0.1}))  # 5 s, scaled
    replayed, real = [], tps._steps
    monkeypatch.setattr(tps, "_steps", lambda m, es: replayed.append(len(es)) or real(m, es))
    rs = await asyncio.gather(*[client.post(CHECK, headers=api_key_headers, json={
        "agent_id": agent.id, "trace_uid": "t1", "candidate": ev(0, "x")}) for _ in range(4)])
    assert [r.status_code for r in rs] == [200] * 4 and len({r.text for r in rs}) == 1
    # the stored events stepped once; four times today
    assert sum(replayed) <= 2_000, sum(replayed)
