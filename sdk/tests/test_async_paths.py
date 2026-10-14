"""Async streaming and pre-check paths (AUDIT-4 A4-2): each mirrors a sync test elsewhere in the suite."""
import asyncio

import httpx
import pytest

from agentops_sdk.async_client import AsyncAgentOpsClient


def seqs(server):
    return [[e["seq"] for e in b["events"]] for b in server.bodies("events")]


def test_async_client_defaults_to_fail_open(aclient, server):
    assert AsyncAgentOpsClient(api_key="k", agent_id="a").fail_closed is False
    server.queue("check", httpx.ConnectError("refused"))
    r = asyncio.run(aclient.acheck("x"))
    assert r.allowed is True and "ConnectError" in r.error


def test_async_check_uses_check_timeout(make_async_client, server):
    c = make_async_client(check_timeout_s=0.5)
    assert asyncio.run(c.acheck("x")).allowed
    assert server.timeouts == [0.5]


@pytest.mark.parametrize("fail_closed", [False, True])
@pytest.mark.parametrize("failure", [
    [(403, {"error": {"code": "policy_block"}})],  # stream dead
    [(503, {}), (503, {})],  # events still pending
])
def test_acheck_with_undelivered_events_skips_the_server(make_async_client, server, fail_closed, failure):
    c = make_async_client(fail_closed=fail_closed)
    server.queue("events", *failure)

    async def run():
        async with c.trace(stream=True) as t:
            t.add_event("tool", "a")
            await asyncio.sleep(0.01)
            return await t.acheck("b")

    r = asyncio.run(run())
    assert (r.allowed, r.error) == (not fail_closed, "trace events not delivered")
    assert "check" not in server.routes()


@pytest.mark.parametrize("failure", [httpx.ConnectError("down"), httpx.ReadTimeout("slow")])
def test_async_stream_retries_after_network_error(aclient, server, failure):
    server.queue("events", failure)

    async def run():
        async with aclient.trace(stream=True) as t:
            t.add_event("tool", "a")
            await asyncio.sleep(0.01)
            t.add_event("tool", "b")
            r = await t.acheck("c")
        return t, r

    t, r = asyncio.run(run())
    assert seqs(server)[:2] == [[0], [0, 1]] and r.allowed and r.error is None
    assert not t._dead and t._sent == 2


def test_async_add_event_without_loop_is_pushed_by_exit(aclient, server):
    t = aclient.trace(stream=True)
    t.add_event("tool", "a")  # no running loop: nothing sent yet
    assert server.requests == []

    async def run():
        async with t:
            t.add_event("tool", "b")

    asyncio.run(run())
    assert [s for b in seqs(server) for s in b] == [0, 1] and t._sent == 2
