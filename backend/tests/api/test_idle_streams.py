"""SG-A6.4 (A6-12): a streamed trace with no append for STREAM_IDLE_S is closed and batch-checked."""
import time
from datetime import timezone

import pytest

import app.services.trace_policy_service as tps
from app.celery_app import celery
from app.models import Trace, TracePolicyViolation
from app.services import trace_service
from app.services.trace_service import OPEN_STREAMS_KEY, STREAM_IDLE_S, close_idle_streams
from app.tasks import policy_tasks
from tests.api.test_event_ingest import CONFIRM, NO_DROP, _append, _policy, _rows, ev


def _later() -> float:
    return time.time() + STREAM_IDLE_S + 1


async def _trace(db, uid):
    (t,) = await _rows(db, Trace, Trace.trace_uid == uid)
    return t


async def test_idle_trace_is_closed_and_checked(client, db, org, agent, api_key_headers, redis, enqueued):
    await _policy(db, org, CONFIRM)
    assert (await _append(client, api_key_headers, agent, "idle", [ev(0, "delete_user")])).status_code == 202
    idle = await _trace(db, "idle")
    await redis.zadd(OPEN_STREAMS_KEY, {idle.id: time.time() - STREAM_IDLE_S - 1})
    assert (await _append(client, api_key_headers, agent, "recent", [ev(0, "search")])).status_code == 202
    recent = await _trace(db, "recent")
    assert await redis.get(f"policy:monitor:{idle.id}")

    assert await close_idle_streams(db) == [idle.id]
    assert enqueued == [idle.id]
    assert (await _trace(db, "idle")).meta == {"stream": "idle"} and (await _trace(db, "recent")).meta == {
        "stream": "open"}
    assert await redis.zrange(OPEN_STREAMS_KEY, 0, -1) == [recent.id]
    assert await redis.get(f"policy:monitor:{idle.id}") is None

    await tps.check_completed_trace(db, idle.id)
    (row,) = await _rows(db, TracePolicyViolation)
    assert (row.counterexample_status, row.counterexample) == ("minimal", [0])
    r = await _append(client, api_key_headers, agent, "idle", [ev(1, "search")])
    assert r.status_code == 409 and r.json()["error"]["code"] == "trace_closed"
    assert await close_idle_streams(db) == [] and enqueued == [idle.id]


async def test_streams_are_tracked_until_final(client, db, org, agent, api_key_headers, redis, enqueued):
    await _policy(db, org, NO_DROP, action="block")
    await _append(client, api_key_headers, agent, "a", [ev(0, "search")])
    await _append(client, api_key_headers, agent, "b", [ev(0, "search")], final=True)
    r = await _append(client, api_key_headers, agent, "c", [ev(0, "drop_db")], final=True)
    assert r.status_code == 403  # refused, so the trace stays open
    a, c = await _trace(db, "a"), await _trace(db, "c")
    assert sorted(await redis.zrange(OPEN_STREAMS_KEY, 0, -1)) == sorted([a.id, c.id])
    before = await redis.zscore(OPEN_STREAMS_KEY, a.id)
    await _append(client, api_key_headers, agent, "a", [ev(1, "search")])
    assert await redis.zscore(OPEN_STREAMS_KEY, a.id) >= before
    await _append(client, api_key_headers, agent, "a", [], final=True)
    assert await redis.zrange(OPEN_STREAMS_KEY, 0, -1) == [c.id]

    assert await close_idle_streams(db, now=_later()) == [c.id]
    (v,) = await _rows(db, TracePolicyViolation, TracePolicyViolation.trace_id == c.id)
    assert v.counterexample_status == "pending"
    assert (await tps.check_completed_trace(db, c.id)) == []  # the refused event was never stored
    assert set(enqueued) == {(await _trace(db, "b")).id, a.id, c.id}


async def test_sweep_skips_a_trace_mid_append(client, db, org, agent, api_key_headers, redis, enqueued):
    await _append(client, api_key_headers, agent, "busy", [ev(0, "search")])
    t = await _trace(db, "busy")
    lock = redis.lock(f"lock:trace-events:{org.id}:busy", timeout=60)
    assert await lock.acquire()
    assert await close_idle_streams(db, now=_later()) == []
    await lock.release()
    await redis.zadd(OPEN_STREAMS_KEY, {t.id: _later()})  # an append landed meanwhile
    assert await close_idle_streams(db, now=_later()) == []
    assert (await _trace(db, "busy")).meta == {"stream": "open"} and enqueued == []
    assert await close_idle_streams(db, now=_later() + STREAM_IDLE_S) == [t.id]


async def test_sweep_drops_stale_entries(db, redis, enqueued):
    await redis.zadd(OPEN_STREAMS_KEY, {"no-such-trace": 0})
    assert await close_idle_streams(db) == []
    assert await redis.zcard(OPEN_STREAMS_KEY) == 0 and enqueued == []


async def test_sweep_survives_redis_errors_on_append(client, db, agent, api_key_headers, redis, monkeypatch):
    async def down(*a, **k):
        raise ConnectionError("redis down")

    monkeypatch.setattr(redis, "zadd", down)
    assert (await _append(client, api_key_headers, agent, "u", [ev(0, "search")])).status_code == 202


async def test_beat_task_closes_idle_streams(client, db, agent, api_key_headers, redis, enqueued, engine,
                                             monkeypatch):
    assert celery.conf.beat_schedule["close-idle-streams-every-5min"]["task"] == policy_tasks.close_idle_streams.name

    async def no_close():
        pass

    monkeypatch.setattr(policy_tasks, "create_async_engine", lambda *a, **k: engine)
    monkeypatch.setattr(policy_tasks, "close_redis", no_close)
    monkeypatch.setattr(trace_service, "STREAM_IDLE_S", -1)
    await _append(client, api_key_headers, agent, "u", [ev(0, "search")])
    assert await policy_tasks._close_idle() == 1
    assert enqueued == [(await _trace(db, "u")).id]


# A8-8: an append landing between the sweep's listing and its lock keeps the trace open
async def test_sweep_skips_a_trace_appended_to_after_listing(client, db, agent, api_key_headers, redis, enqueued,
                                                              monkeypatch):
    await _append(client, api_key_headers, agent, "busy", [ev(0, "search")])
    t = await _trace(db, "busy")
    listed = redis.zrangebyscore

    async def append_after_listing(*a, **kw):
        ids = await listed(*a, **kw)
        await redis.zadd(OPEN_STREAMS_KEY, {t.id: _later()})
        return ids

    monkeypatch.setattr(redis, "zrangebyscore", append_after_listing)
    assert await close_idle_streams(db, now=_later()) == []
    assert (await _trace(db, "busy")).meta == {"stream": "open"} and enqueued == []


# A8-7: an idle-closed trace is a timeout, with its latency up to its last append
async def test_idle_closed_trace_is_a_timeout(client, db, agent, api_key_headers, redis, enqueued):
    await _append(client, api_key_headers, agent, "t", [ev(0, "search")])
    t = await _trace(db, "t")
    await redis.zadd(OPEN_STREAMS_KEY, {t.id: t.created_at.replace(tzinfo=timezone.utc).timestamp() + 5})
    assert await close_idle_streams(db, now=_later() + 5) == [t.id]
    t = await _trace(db, "t")
    assert t.status == "timeout" and abs(t.latency_ms - 5000) <= 1


# A8-8: the batch fills a pending live row only when it was found by the same policy version
async def test_batch_leaves_an_older_versions_pending_row(client, db, org, agent, api_key_headers, enqueued):
    p = await _policy(db, org, NO_DROP)
    await _append(client, api_key_headers, agent, "u1", [ev(0, "drop_db")])
    p.version = 2
    await db.commit()
    await _append(client, api_key_headers, agent, "u1", [], final=True)
    (tid,) = enqueued
    await tps.check_completed_trace(db, tid)
    rows = await _rows(db, TracePolicyViolation)
    assert sorted((r.policy_version, r.at_seq, r.counterexample_status) for r in rows) == [
        (1, 0, "none"), (2, 0, "minimal")]
