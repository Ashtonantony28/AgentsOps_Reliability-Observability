import asyncio
import json
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
    bodies = server.bodies("events")
    assert bodies[0]["agent_id"] == "a1" and all(b["environment"] == client.environment for b in bodies)
    assert ["input" in b for b in bodies] == [True, False, False, False]
    assert ["output" in b for b in bodies] == [False, False, False, True]
    assert server.requests[0][2]["x-api-key"] == client.api_key
    assert client._queue.empty()  # no legacy whole-trace payload


def test_stream_sends_trace_fields(client, server):
    with pytest.raises(RuntimeError):
        with client.trace(stream=True, input="q", session_id="s1", environment="prod") as t:
            t.set_metadata(user="u7")
            t.add_event("tool", "a")
            t.set_output("partial", tokens_input=3, tokens_output=4, cost_usd=0.5)
            t.set_metadata(step=2)
            raise RuntimeError("boom")
    first, last = server.bodies("events")
    assert {k: first[k] for k in ("environment", "agent_version", "session_id", "input", "metadata")} == {
        "environment": "prod", "agent_version": "1.0.0", "session_id": "s1", "input": "q",
        "metadata": {"user": "u7"}}
    assert {k: last[k] for k in ("final", "output", "status", "error_message", "tokens_input", "tokens_output",
                                 "cost_usd", "metadata")} == {
        "final": True, "output": "partial", "status": "error", "error_message": "RuntimeError: boom",
        "tokens_input": 3, "tokens_output": 4, "cost_usd": 0.5, "metadata": {"user": "u7", "step": 2}}
    assert last["latency_ms"] >= 0 and "input" not in last


def test_checks_send_the_trace_environment(client, server):
    client.check("a")
    with client.trace(environment="prod") as t:
        t.check("a")
    with client.trace(stream=True, environment="staging") as t:
        t.check("a")
    assert [b["environment"] for b in server.bodies("check")] == [client.environment, "prod", "staging"]


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
    (409, {"error": {"code": "seq_conflict", "message": "gap", "details": {"expected_seq": 4}}}),
    (409, {"error": {"code": "trace_closed"}}),
    (409, {"error": {"code": "agent_mismatch"}}),
    (413, {"error": {"code": "trace_too_long", "details": {"max_events": 10_000}}}),
    (422, {"error": {"code": "validation_error"}}),
])
def test_stops_streaming_on_rejection_but_keeps_checking(client, server, caplog, failure):
    server.queue("events", failure)
    server.queue("check", (200, {"allowed": False, "violations": [{"policy_name": "p"}]}))
    with caplog.at_level(logging.WARNING, logger="agentops_sdk"):
        with client.trace(stream=True) as t:
            t.add_event("tool", "a")
            t.add_event("tool", "b")
            result = t.check("c")
    assert seqs(server) == [[0]]
    assert server.routes() == ["events", "check"]  # checked against the events the server holds
    body = server.bodies("check")[0]
    assert body["trace_uid"] == t.trace_id
    assert [e["name"] for e in body["prefix"]] == ["a", "b"] and body["candidate"]["seq"] == 2  # undelivered events
    assert not result.allowed and result.error is None
    assert client._queue.empty()
    assert "stopped streaming" in caplog.text and caplog.text.count("stopped streaming") == 1


BLOCK = (403, {"error": {"code": "policy_block", "message": "trace policy 'p' violated at seq 0"}})


def test_policy_block_drops_the_refused_event_and_streams_on(client, server, caplog):
    server.queue("events", BLOCK)
    with caplog.at_level(logging.WARNING, logger="agentops_sdk"):
        with client.trace(stream=True) as t:
            t.add_event("tool", "a")
            t.add_event("tool", "b")
            assert t.check("c").allowed
    assert seqs(server) == [[0], [0], []] and finals(server) == [False, False, True]
    assert server.routes() == ["events", "events", "check", "events"]
    assert server.bodies("check")[0]["candidate"]["seq"] == 1
    assert [(e["seq"], e["name"]) for e in t.events] == [(0, "b")] and not t._dead
    assert "refused by policy: trace policy 'p' violated at seq 0" in caplog.text


def test_refused_batch_is_resent_one_event_at_a_time(client, server):
    server.queue("events", (500, {}), BLOCK, (202, {"accepted": 1}), BLOCK)
    with client.trace(stream=True) as t:
        t.add_event("tool", "a")
        t.add_event("tool", "b")  # [a, b] refused: a alone is accepted, b alone refused
        t.add_event("tool", "c")
    assert seqs(server) == [[0], [0, 1], [0], [1], [1], []]
    assert [(e["seq"], e["name"]) for e in t.events] == [(0, "a"), (1, "c")]
    assert t._sent == 2


def test_refused_final_batch_still_closes_the_trace(client, server):
    server.queue("events", (503, {}), (503, {}), BLOCK, (202, {"accepted": 1}), BLOCK)
    with client.trace(stream=True) as t:
        t.add_event("tool", "a")
        t.add_event("tool", "b")
    assert seqs(server) == [[0], [0, 1], [0, 1], [0], [1], []]
    assert finals(server) == [False, False, True, False, True, True]
    assert [e["name"] for e in t.events] == ["a"]


def test_trace_whose_every_event_is_refused_is_closed_not_reuploaded(client, server):
    server.queue("events", BLOCK)
    with client.trace(stream=True) as t:
        t.add_event("tool", "a")
    assert seqs(server) == [[0], []] and finals(server) == [False, True]
    assert t.events == [] and client._queue.empty()


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


def test_async_client_streams_on_after_policy_block(aclient, server):
    server.queue("events", BLOCK)

    async def run():
        async with aclient.trace(stream=True) as t:
            t.add_event("tool", "a")
            await asyncio.sleep(0.01)
            t.add_event("tool", "b")
            return t, await t.acheck("c")

    t, r = asyncio.run(run())
    assert seqs(server) == [[0], [0], []] and r.allowed and r.error is None
    assert [e["name"] for e in t.events] == ["b"]


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


def test_refused_event_in_a_batch_renumbers_the_rest(client, server):
    server.queue("events", (500, {}), BLOCK, BLOCK)
    with client.trace(stream=True) as t:
        t.add_event("tool", "a")
        t.add_event("tool", "b")  # [a, b] refused, then a alone refused: b becomes seq 0
    assert seqs(server) == [[0], [0, 1], [0], [0], []]
    assert [(e["seq"], e["name"]) for e in t.events] == [(0, "b")]


def test_refusal_settled_by_an_overlapping_push_drops_nothing(client):
    t = client.trace(stream=True)
    t.events.append({"seq": 0, "name": "a"})  # recorded, not pushed
    assert t._settle("refused", 0, [{"seq": 0, "name": "a"}], False) is True
    assert t.events == [{"seq": 0, "name": "a"}] and t._refused == 0


def test_fail_closed_client_still_fails_checks_after_a_rejection(make_client, server):
    c = make_client(fail_closed=True)
    server.queue("events", (413, {"error": {"code": "trace_too_long"}}))
    with c.trace(stream=True) as t:
        t.add_event("tool", "a")
        result = t.check("b")
    assert (result.allowed, result.error) == (False, "trace events not delivered")
    assert "check" not in server.routes()


def test_refused_empty_final_push_ends_the_push(client, server):
    server.queue("events", (202, {"accepted": 1}), BLOCK)
    with client.trace(stream=True) as t:
        t.add_event("tool", "a")
    assert seqs(server) == [[0], []] and [e["name"] for e in t.events] == ["a"]


def test_block_on_trace_text_stops_streaming_and_keeps_events(client, server):
    server.queue("events", (403, {"error": {"code": "policy_block", "message": "PII detected",
                                            "details": {"rule": "no-pii", "target": "trace"}}}))
    with client.trace(stream=True, input="mail jane.doe@example.com") as t:
        t.add_event("tool", "a")
        t.add_event("tool", "b")
    assert seqs(server) == [[0]] and [e["name"] for e in t.events] == ["a", "b"] and t._dead


def test_backlog_over_the_work_bound_is_split(client, server):
    """SG-A10.1: an append that is too much policy work is sent again in appends of the size the server says fits."""
    too_much = (413, {"error": {"code": "policy_work_too_large", "message": "x", "details": {"max_events": 2}}})
    server.queue("events", (500, {}), (500, {}), (500, {}), too_much)
    with client.trace(stream=True) as t:
        for name in "abcde":
            t.add_event("tool", name)
    assert seqs(server) == [[0], [0, 1], [0, 1, 2], [0, 1, 2, 3], [0, 1], [2, 3], [4], []]
    assert t._sent == 5 and not t._dead


def test_other_413_on_append_stops_streaming(client, server):
    server.queue("events", (413, "request too large"))
    with client.trace(stream=True) as t:
        t.add_event("tool", "a")
    assert t._dead


def too_large(limit=16_777_216, **details):
    return 413, {"error": {"code": "request_too_large", "message": f"a request body is at most {limit} bytes",
                           "details": {"error": "request_too_large", "max_bytes": limit, **details}}}


def body_limit(server, limit):
    """The server's body limit (SG-A13.2): an append over `limit` bytes is a 413; returns the appends taken."""
    reply, taken = server.reply, []

    def limited(request):
        if len(request.content) > limit:
            server.requests.append((request.url.path, json.loads(request.content), dict(request.headers)))
            status, body = too_large(limit)
            return httpx.Response(status, json=body)
        r = reply(request)
        if r.is_success:
            taken.append(json.loads(request.content))
        return r

    server.reply = limited
    return taken


def delivered(taken):
    return [(e["seq"], e["name"]) for b in taken for e in b["events"]]


def test_backlog_over_the_body_limit_is_sent_in_halves(client, server):
    """SG-A14.1: an append over the server's body limit is sent again in smaller appends, the stream going on."""
    server.queue("events", (500, {}), (500, {}), (500, {}), too_large(), too_large())
    with client.trace(stream=True) as t:
        for name in "abcde":
            t.add_event("tool", name)
    assert seqs(server) == [[0], [0, 1], [0, 1, 2], [0, 1, 2, 3], [0, 1], [0], [1], [2], [3], [4], []]
    assert finals(server)[-1] is True and t._sent == 5 and not t._dead and t._max_batch == 1


def test_body_limit_without_max_bytes_still_splits(client, server):
    server.queue("events", (500, {}), (413, {"error": {"code": "request_too_large"}}))
    with client.trace(stream=True) as t:
        t.add_event("tool", "a")
        t.add_event("tool", "b")
    assert seqs(server) == [[0], [0, 1], [0], [1], []] and not t._dead


@pytest.mark.parametrize("sizes", [[10, 10, 10, 10], [1, 5_000, 1, 1, 9_000, 1], [3_000] * 7, [9_000, 1, 1, 1, 1]])
def test_stream_under_a_body_limit_delivers_all_but_oversized_events(client, server, sizes):
    """Against a real byte limit: every event that fits is delivered once, in order and renumbered; one that alone
    is over the limit is dropped as a refused one; the final status arrives."""
    taken = body_limit(server, 4_000)
    server.queue("events", *[(503, {})] * 3)  # an outage: the backlog grows past the limit
    with client.trace(stream=True) as t:
        for i, n in enumerate(sizes):
            t.add_event("llm", f"e{i}", output={"text": "x" * n})
    kept = [f"e{i}" for i, n in enumerate(sizes) if n < 4_000]
    assert delivered(taken) == list(enumerate(kept)) and [e["name"] for e in t.events] == kept
    assert taken[-1]["final"] is True and taken[-1]["status"] == "success" and [b["final"] for b in taken].count(True) == 1
    assert not t._dead and t._refused == len(sizes) - len(kept)


@pytest.mark.parametrize("sizes", [[1_000] * 9, [1, 5_000, 1, 1, 1]])
def test_async_stream_under_a_body_limit_delivers_all_but_oversized_events(aclient, server, sizes):
    taken = body_limit(server, 4_000)
    server.queue("events", *[(503, {})] * 3)

    async def run():
        async with aclient.trace(stream=True) as t:
            for i, n in enumerate(sizes):
                t.add_event("llm", f"e{i}", output={"text": "x" * n})
                await asyncio.sleep(0)
        return t

    t = asyncio.run(run())
    kept = [f"e{i}" for i, n in enumerate(sizes) if n < 4_000]
    assert delivered(taken) == list(enumerate(kept)) and [e["name"] for e in t.events] == kept
    assert taken[-1]["final"] is True and not t._dead and t._refused == len(sizes) - len(kept)


def test_async_backlog_over_the_body_limit_is_split(aclient, server):
    """The A14-2 sequence through the async client."""
    server.queue("events", (500, {}), (500, {}), (500, {}), too_large())

    async def run():
        async with aclient.trace(stream=True) as t:
            for name in "abcde":
                t.add_event("tool", name)
                await asyncio.sleep(0.01)
        return t

    t = asyncio.run(run())
    assert seqs(server) == [[0], [0, 1], [0, 1, 2], [0, 1, 2, 3], [0, 1], [2, 3], [4], []]
    assert t._sent == 5 and not t._dead


def test_single_event_over_the_body_limit_is_dropped(client, server, caplog):
    taken = body_limit(server, 4_000)
    with caplog.at_level(logging.WARNING, logger="agentops_sdk"):
        with client.trace(stream=True) as t:
            t.add_event("tool", "a")
            t.add_event("tool", "big", input={"text": "x" * 5_000})
            t.add_event("tool", "c")
    assert seqs(server) == [[0], [1], [1], []] and delivered(taken) == [(0, "a"), (1, "c")]
    assert taken[-1]["final"] is True and not t._dead and t._refused == 1
    assert "over the server's body limit" in caplog.text


@pytest.mark.parametrize("big", ["input", "output"])
def test_trace_fields_over_the_body_limit_stop_the_stream(make_client, server, big):
    """When the trace's own fields, not an event, pass the limit, no event is dropped for them: the stream stops."""
    taken = body_limit(server, 4_000)
    with make_client().trace(stream=True, **({"input": "x" * 5_000} if big == "input" else {})) as t:
        t.add_event("tool", "a")
        t.add_event("tool", "b")
        if big == "output":
            t.set_output("x" * 5_000)
    assert t._dead and [e["name"] for e in t.events] == ["a", "b"] and t._refused == 0
    assert delivered(taken) == ([] if big == "input" else [(0, "a"), (1, "b")])


def test_other_413_codes_on_append_stop_streaming(client, server):
    server.queue("events", (413, {"error": {"code": "something_else", "details": {"max_bytes": 10}}}))
    with client.trace(stream=True) as t:
        t.add_event("tool", "a")
        t.add_event("tool", "b")
    assert t._dead and seqs(server) == [[0]]


@pytest.mark.parametrize("details, expected", [
    ({"max_bytes": 16_777_216}, ("max_bytes", 16_777_216)),
    ({}, ("max_bytes", None)), ({"max_bytes": 0}, ("max_bytes", None)), ({"max_bytes": True}, ("max_bytes", None)),
])
def test_push_outcome_on_the_body_limit(details, expected):
    from agentops_sdk.precheck import push_outcome
    r = httpx.Response(413, json={"error": {"code": "request_too_large", "details": details}})
    assert push_outcome(r) == expected
    assert push_outcome(httpx.Response(413, text="<html>413</html>")) == "fatal"
