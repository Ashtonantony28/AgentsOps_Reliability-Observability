"""AUDIT-3.md SDK findings as failing tests; each is xfail(strict=True) until its sub-goal fixes it."""
import asyncio

import pytest

from agentops_sdk import PolicyBlocked, guard
from agentops_sdk.utils import drain_disk

BLOCK = (200, {"allowed": False, "violations": [{"policy_id": "p1", "policy_name": "no-refunds"}]})


@pytest.mark.xfail(strict=True, reason="A3-4: a sync check in an async streamed trace skips the server and fails open")
def test_sync_guard_in_async_streamed_trace_is_checked(aclient, server):
    server.queue("check", BLOCK)
    calls = []

    @guard
    def refund():
        calls.append(1)

    async def run():
        async with aclient.trace(stream=True) as t:
            t.add_event("tool", "search")
            with pytest.raises(PolicyBlocked):
                refund()

    asyncio.run(run())
    assert calls == [] and "check" in server.routes()


@pytest.mark.xfail(strict=True, reason="A3-10: traces the bulk endpoint rejects with storage_error are dropped")
def test_bulk_storage_error_is_retried(client, server):
    server.queue("bulk", (202, {"accepted": [], "rejected": [{"index": 0, "reason": "storage_error"}]}))
    with client.trace() as t:
        t.add_tool_call("search")
    client.flush()
    assert [p["trace_id"] for p in drain_disk(client._disk)] == [t.trace_id]
