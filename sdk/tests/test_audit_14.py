"""AUDIT-14: the server's 503 check_timeout (SG-A12.1) as both clients and @guard see it; it follows fail_closed like
the SDK's own timeout, never blocks a fail-open client nor allows a fail-closed one."""
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
