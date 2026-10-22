"""AUDIT-14: the server's 503 check_timeout (SG-A12.1) as both clients and @guard see it; it follows fail_closed like
the SDK's own timeout, never blocks a fail-open client nor allows a fail-closed one. A14-2, a failing test
fixed by SG-A14.1."""
import asyncio

import pytest

from agentops_sdk import PolicyBlocked, guard

STOPPED = (503, {"error": {"code": "check_timeout", "message": "policy check stopped; resend it to resume",
                           "details": {}}})


@pytest.mark.parametrize("fail_closed", [False, True])
def test_check_timeout_follows_fail_closed(make_client, make_async_client, server, fail_closed):
    c, ac = make_client(fail_closed=fail_closed), make_async_client(fail_closed=fail_closed)
    server.queue("check", STOPPED, STOPPED)
    for r in (c.check("x"), asyncio.run(ac.acheck("x"))):
        assert (r.allowed, r.violations, r.error) == (not fail_closed, [], "http 503")


@pytest.mark.parametrize("fail_closed", [False, True])
def test_guard_on_check_timeout(make_async_client, server, fail_closed):
    ac, calls = make_async_client(fail_closed=fail_closed), []

    @guard
    async def tool():
        calls.append(1)

    async def run():
        async with ac.trace() as t:
            if fail_closed:
                with pytest.raises(PolicyBlocked, match="http 503"):
                    await tool()
            else:
                await tool()
        return t

    server.queue("check", STOPPED)
    t = asyncio.run(run())
    assert calls == ([] if fail_closed else [1]) and len(t.events) == len(calls)


def seqs(server) -> list:
    return [[e["seq"] for e in b["events"]] for b in server.bodies("events")]


# A14-2: since SG-A13.2 (#58) an append body over 16 MiB is 413 request_too_large; a stream's backlog (all pending
# events go in one append) passes it after an outage with large payloads, and the SDK ends the stream on it: the rest
# of the trace, its final status included, never reaches the server, where a split (as for max_events) delivers it
def test_backlog_over_the_body_limit_is_split_not_dropped(client, server):
    too_big = (413, {"error": {"code": "request_too_large", "message": "request body over 16777216 bytes",
                               "details": {"max_bytes": 16_777_216}}})
    server.queue("events", (500, {}), (500, {}), (500, {}), too_big)
    with client.trace(stream=True) as t:
        for name in "abcde":
            t.add_event("tool", name)
    assert seqs(server)[:4] == [[0], [0, 1], [0, 1, 2], [0, 1, 2, 3]]  # control: the backlog grew to 4 events
    assert t._sent == 5 and not t._dead, (seqs(server), t._sent)
