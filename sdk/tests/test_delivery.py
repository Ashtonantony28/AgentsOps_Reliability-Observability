"""SG-A6.1: both clients deliver every queued trace, in requests the server accepts."""
import asyncio
import time
import uuid

import httpx

from agentops_sdk.utils import MAX_REQUEST_EVENTS, batches, drain_disk


def _payload(events: int = 1, key: str = "events") -> dict:
    return {"trace_id": str(uuid.uuid4()), "agent_id": "a1", key: [{"seq": i, "name": "t"} for i in range(events)]}


def _sizes(server) -> list[tuple[int, int]]:
    return [(len(b["traces"]), sum(len(t.get("events", [])) + len(t.get("tool_calls", [])) for t in b["traces"]))
            for b in server.bodies("bulk")]


def test_batches_split_by_count_and_events():
    items = [_payload(4), _payload(4), _payload(3, "tool_calls"), _payload(1), _payload(0)]
    assert [[len(t.get("events", t.get("tool_calls"))) for t in b] for b in batches(items, 2, max_events=8)] == \
        [[4, 4], [3, 1], [0]]
    assert list(batches(items, 3, max_events=7)) == [items[:1], items[1:3], items[3:]]
    assert list(batches([], 2)) == []


def test_batches_take_a_trace_at_exactly_the_cap():
    items = [_payload(MAX_REQUEST_EVENTS), _payload(1)]
    assert list(batches(items, 50)) == [items[:1], items[1:]]


def test_batches_drop_a_trace_over_the_cap(caplog):
    big = _payload(MAX_REQUEST_EVENTS + 1)
    small = _payload(1)
    assert list(batches([big, small], 50)) == [[small]]
    assert f"dropped trace {big['trace_id']}: {MAX_REQUEST_EVENTS + 1} events" in caplog.text


def test_flush_sends_every_batch(make_client, server):
    c = make_client(batch_size=2)
    for _ in range(5):
        c.enqueue(_payload())
    c.flush()
    assert _sizes(server) == [(2, 2), (2, 2), (1, 1)]
    assert c._queue.empty()


def test_over_cap_trace_is_not_buffered(client, server, caplog):
    client.enqueue(_payload(MAX_REQUEST_EVENTS + 1))
    client.enqueue(_payload(1))
    client.flush()
    assert _sizes(server) == [(1, 1)]
    assert drain_disk(client._disk) == []


def test_close_buffers_what_it_cannot_send(make_client, server):
    c = make_client(batch_size=2)
    server.queue("bulk", (200, {"accepted": 2, "rejected": []}), (503, {}))
    for _ in range(3):
        c.enqueue(_payload())
    c.close()
    assert _sizes(server) == [(2, 2), (1, 1)]
    assert len(drain_disk(c._disk)) == 1
    c.close()  # idempotent
    assert len(server.bodies("bulk")) == 2


def test_unreachable_server_is_tried_once_per_flush(make_client, make_async_client, server):
    c, ac = make_client(batch_size=2), make_async_client(batch_size=2)
    server.queue("bulk", httpx.ConnectError("down"), httpx.ConnectError("down"))
    for _ in range(5):
        c.enqueue(_payload())
        ac.enqueue(_payload())
    c.flush()
    asyncio.run(ac.flush())
    assert len(server.bodies("bulk")) == 2
    assert len(drain_disk(c._disk)) == len(drain_disk(ac._disk)) == 5


def test_flusher_thread_sends_the_whole_queue(make_client, server):
    c = make_client(batch_size=2, flush_interval_s=0.01)
    for _ in range(3):
        c.enqueue(_payload())
    for _ in range(200):
        if c._queue.empty() and sum(n for n, _ in _sizes(server)) == 3:
            break
        time.sleep(0.01)
    c.close()
    sizes = _sizes(server)
    assert sum(n for n, _ in sizes) == 3 and max(n for n, _ in sizes) <= 2


def test_async_flush_respects_batch_size_and_cap(make_async_client, server):
    c = make_async_client(batch_size=2)
    for p in [_payload(MAX_REQUEST_EVENTS - 1), _payload(2), _payload(MAX_REQUEST_EVENTS + 1), _payload(), _payload()]:
        c.enqueue(p)
    asyncio.run(c.flush())
    assert _sizes(server) == [(1, MAX_REQUEST_EVENTS - 1), (2, 3), (1, 1)]
    assert drain_disk(c._disk) == []


def test_async_close_delivers_every_queued_trace(make_async_client, server):
    c = make_async_client(batch_size=2)

    async def run():
        await c.start()
        for _ in range(5):
            await c.aenqueue(_payload())
        await c.close()

    asyncio.run(run())
    assert sum(n for n, _ in _sizes(server)) == 5 and max(n for n, _ in _sizes(server)) <= 2


def test_async_periodic_flusher_sends_without_flush(make_async_client, server):
    c = make_async_client(flush_interval_s=0.01)

    async def run():
        await c.start()
        await c.aenqueue(_payload())
        for _ in range(200):
            if server.bodies("bulk"):
                break
            await asyncio.sleep(0.01)
        sent = len(server.bodies("bulk"))
        c._stop.set()
        await c._task
        return sent

    assert asyncio.run(run()) == 1


def test_streamed_trace_id_is_quoted_in_the_path(client, server):
    with client.trace(stream=True) as t:
        t.trace_id = "a/b?c"
        t.add_event("tool", "x")
    assert server.routes() == ["events", "events"]  # the event, then the final push
    assert {p for p, _, _ in server.requests} == {"/api/traces/a/b?c/events"}  # decoded: one path segment, no query


def test_overlapping_pushes_do_not_resend_settled_events(make_async_client, server):
    c = make_async_client()
    server.delay = 0.02  # the async push of event 0 is still in flight when the sync check pushes 0 and 1

    async def run():
        async with c.trace(stream=True) as t:
            t.add_event("tool", "a")
            await asyncio.sleep(0)
            t.add_event("tool", "b")
            t.check("x")
            await asyncio.gather(*t._tasks)
            return t._sent

    assert asyncio.run(run()) == 2
    assert sorted(len(b["events"]) for b in server.bodies("events") if b["events"]) == [1, 2]
