import asyncio
import logging

import httpx
import pytest


def seqs(server):
    return [[e["seq"] for e in b["events"]] for b in server.bodies("events")]


def finals(server):
    return [b["final"] for b in server.bodies("events")]


def test_streams_each_event_in_order(client, server):
    with client.trace(stream=True) as t:
        for name in ("a", "b", "c"):
            t.add_event("tool", name)
        assert seqs(server) == [[0], [1], [2]]
    assert seqs(server) == [[0], [1], [2], []]
    assert finals(server) == [False, False, False, True]
    paths = {p for p, _, _ in server.requests}
    assert paths == {f"/api/traces/{t.trace_id}/events"}
    body = server.bodies("events")[0]
    assert set(body) == {"agent_id", "events", "final"} and body["agent_id"] == "a1"
    assert server.requests[0][2]["x-api-key"] == client.api_key
    assert client._queue.empty()  # no legacy whole-trace payload


@pytest.mark.parametrize("failure", [
    httpx.ConnectError("down"),
    (503, {"error": {"code": "unavailable"}}),
    (429, {"error": {"code": "rate_limited"}}),
    (409, {"error": {"code": "trace_busy", "message": "retry"}}),
])
def test_retries_pending_on_transient_failure(client, server, failure):
    server.queue("events", failure)
    with client.trace(stream=True) as t:
        t.add_event("tool", "a")
        t.add_event("tool", "b")
        t.add_event("tool", "c")
    assert seqs(server) == [[0], [0, 1], [2], []]
    assert finals(server) == [False, False, False, True]
    assert client._queue.empty()


def test_pending_flushed_with_final(client, server):
    server.queue("events", (500, {}))
    with client.trace(stream=True) as t:
        t.add_event("tool", "a")
    assert seqs(server) == [[0], [0]]
    assert finals(server) == [False, True]


@pytest.mark.parametrize("failure", [
    (403, {"error": {"code": "policy_block", "message": "blocked"}}),
    (409, {"error": {"code": "seq_conflict", "message": "gap", "details": {"expected_seq": 4}}}),
    (409, {"error": {"code": "trace_closed"}}),
    (422, {"error": {"code": "validation_error"}}),
])
def test_stops_streaming_on_rejection(client, server, caplog, failure):
    server.queue("events", failure)
    with caplog.at_level(logging.WARNING, logger="agentops_sdk"):
        with client.trace(stream=True) as t:
            t.add_event("tool", "a")
            t.add_event("tool", "b")
            result = t.check("c")
    assert seqs(server) == [[0]]
    assert server.routes() == ["events"]  # the server lacks the trace's events, so the check fails open
    assert result.allowed and result.error == "trace events not delivered"
    assert client._queue.empty()
    assert "stopped streaming" in caplog.text and caplog.text.count("stopped streaming") == 1


def test_final_push_failure_logs_undelivered(client, server, caplog):
    server.queue("events", (202, {"accepted": 1}), *[httpx.ConnectError("x")] * 3)
    with caplog.at_level(logging.WARNING, logger="agentops_sdk"):
        with client.trace(stream=True) as t:
            t.add_event("tool", "a")
            t.add_event("tool", "b")
            t.add_event("tool", "c")
    assert finals(server) == [False, False, False, True]
    assert "2 events not delivered" in caplog.text


def test_streamed_trace_without_events_falls_back_to_legacy(client, server):
    with client.trace(stream=True, input="q") as t:
        t.set_output("done")
    (p,) = client._drain()
    assert p["trace_id"] == t.trace_id and "events" not in p
    assert server.requests == []


def test_error_inside_streamed_trace_still_closes(client, server):
    with pytest.raises(RuntimeError):
        with client.trace(stream=True) as t:
            t.add_event("tool", "a")
            raise RuntimeError("boom")
    assert finals(server) == [False, True]
    assert t.status == "error"


def test_trace_check_pushes_then_checks_with_history(client, server):
    server.queue("events", httpx.ConnectError("x"))
    with client.trace(stream=True) as t:
        t.add_event("tool", "a")
        t.add_event("llm", "b")
        r = t.check("c", input={"k": 1})
    assert r.allowed
    assert server.routes() == ["events", "events", "check", "events"]
    assert seqs(server) == [[0], [0, 1], []]
    check = server.bodies("check")[0]
    assert check["trace_uid"] == t.trace_id and check["agent_id"] == "a1"
    assert check["candidate"]["seq"] == 2 and check["candidate"]["input"] == {"k": 1}


def test_sync_client_under_async_with(client, server):
    async def run():
        async with client.trace(stream=True) as t:
            t.add_event("tool", "a")
            server.queue("events", (500, {}))
            t.add_event("tool", "b")
            r = await t.acheck("c")
        return t, r

    t, r = asyncio.run(run())
    assert r.allowed
    assert server.routes() == ["events", "events", "events", "check", "events"]
    assert seqs(server) == [[0], [1], [1], []]
    assert server.bodies("check")[0]["candidate"]["seq"] == 2
    assert client._queue.empty()


def test_async_client_streams_in_order(aclient, server):
    server.delay = 0.005

    async def run():
        async with aclient.trace(stream=True) as t:
            for i in range(5):
                t.add_event("tool", f"e{i}")
                if i % 2:
                    await asyncio.sleep(0)
            await asyncio.sleep(0.02)
        return t

    t = asyncio.run(run())
    flat = [s for batch in seqs(server) for s in batch]
    assert flat == [0, 1, 2, 3, 4]
    assert finals(server)[-1] is True and finals(server).count(True) == 1
    assert aclient._queue.empty()


def test_async_client_exit_awaits_pending_push(aclient, server):
    async def run():
        async with aclient.trace(stream=True) as t:
            t.add_event("tool", "a")
            t.add_event("tool", "b")
        return t

    asyncio.run(run())
    flat = [s for batch in seqs(server) for s in batch]
    assert flat == [0, 1] and finals(server)[-1] is True


def test_async_client_acheck_awaits_push(aclient, server):
    server.queue("events", (503, {}))

    async def run():
        async with aclient.trace(stream=True) as t:
            t.add_event("tool", "a")
            await asyncio.sleep(0.01)
            t.add_event("tool", "b")
            r = await t.acheck("c")
            assert seqs(server)[-1] == [0, 1]
        return t, r

    t, r = asyncio.run(run())
    assert server.routes()[-2:] == ["check", "events"]
    check = server.bodies("check")[0]
    assert check["trace_uid"] == t.trace_id and check["candidate"]["seq"] == 2


def test_async_client_stops_on_policy_block(aclient, server):
    server.queue("events", (403, {"error": {"code": "policy_block"}}))

    async def run():
        async with aclient.trace(stream=True) as t:
            t.add_event("tool", "a")
            await asyncio.sleep(0.01)
            t.add_event("tool", "b")

    asyncio.run(run())
    assert seqs(server) == [[0]]


def test_module_trace_stream_flag(singleton, server):
    import agentops_sdk
    c = singleton()
    with agentops_sdk.trace(stream=True) as t:
        t.add_event("tool", "a")
    assert t.stream and seqs(server) == [[0], []]
    assert agentops_sdk.atrace(stream=True).stream
    assert c._queue.empty()


def test_resyncs_when_the_server_already_stored_a_batch(client, server):
    server.queue("events", (409, {"error": {"code": "seq_conflict", "message": "x", "details": {"expected_seq": 1}}}))
    with client.trace(stream=True) as t:
        t.add_event("tool", "a")
        t.add_event("tool", "b")
    assert seqs(server) == [[0], [1], []]
    assert finals(server) == [False, False, True]


def test_resync_resends_the_rest_of_the_batch(client, server):
    conflict = (409, {"error": {"code": "seq_conflict", "message": "x", "details": {"expected_seq": 1}}})
    server.queue("events", (500, {}), conflict)
    with client.trace(stream=True) as t:
        t.add_event("tool", "a")
        t.add_event("tool", "b")
    assert seqs(server) == [[0], [0, 1], [1], []]


@pytest.mark.parametrize("fail_closed", [False, True])
def test_check_with_undelivered_events_follows_failure_policy(client, server, fail_closed):
    client.fail_closed = fail_closed
    server.queue("events", (503, {}), (503, {}))
    with client.trace(stream=True) as t:
        t.add_event("tool", "a")
        result = t.check("b")
    assert (result.allowed, result.error) == (not fail_closed, "trace events not delivered")
    assert "check" not in server.routes()


def test_sync_check_overlapping_async_push_keeps_order(aclient, server):
    server.delay = 0.05
    server.queue("events", (202, {"accepted": 1}),
                 (409, {"error": {"code": "seq_conflict", "details": {"expected_seq": 1}}}))

    async def run():
        async with aclient.trace(stream=True) as t:
            t.add_event("tool", "a")
            await asyncio.sleep(0)  # the async push of [0] is in flight
            assert t.check("b").allowed  # pushes [0] synchronously, then checks
            t.add_event("tool", "b")
        return t

    t = asyncio.run(run())
    assert server.routes()[:2] == ["events", "check"]
    assert seqs(server) == [[0], [0], [1], []] and finals(server)[-1] is True
    assert not t._dead and t._sent == 2
