"""AUDIT-3.md SDK findings as failing tests; each is xfail(strict=True) until its sub-goal fixes it."""
import asyncio

import pytest

from agentops_sdk import PolicyBlocked, guard
from agentops_sdk.async_client import AsyncAgentOpsClient
from agentops_sdk.utils import drain_disk

BLOCK = (200, {"allowed": False, "violations": [{"policy_id": "p1", "policy_name": "no-refunds"}]})


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


def test_bulk_storage_error_is_retried(client, server):
    server.queue("bulk", (202, {"accepted": [], "rejected": [{"index": 0, "reason": "storage_error"}]}))
    with client.trace() as t:
        t.add_tool_call("search")
    client.flush()
    assert [p["trace_id"] for p in drain_disk(client._disk)] == [t.trace_id]


def test_bulk_policy_block_is_logged_not_retried(client, server, caplog):
    server.queue("bulk", (202, {"accepted": [], "rejected": [{"index": 0, "reason": "policy_block", "rule": "r"}]}))
    with client.trace() as t:
        t.add_tool_call("search")
    client.flush()
    assert drain_disk(client._disk) == []
    assert f"dropped trace {t.trace_id}: policy_block" in caplog.text


def test_async_bulk_storage_error_is_resent_by_next_client(aclient, server):
    server.queue("bulk", (202, {"accepted": [], "rejected": [{"index": 0, "reason": "storage_error"}]}))

    async def run():
        async with aclient.trace() as t:
            t.add_tool_call("search")
        await aclient.flush()
        return t

    t = asyncio.run(run())
    nxt = AsyncAgentOpsClient(api_key=aclient.api_key, agent_id="a1", base_url="http://test")
    assert [nxt._queue.get_nowait()["trace_id"]] == [t.trace_id] and nxt._queue.empty()
