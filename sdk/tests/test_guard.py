import asyncio

import httpx
import pytest

import agentops_sdk
from agentops_sdk import PolicyBlocked, guard

BLOCK = (200, {"allowed": False, "violations": [{"policy_id": "p1", "policy_name": "no-refunds"}]})


def test_guard_without_trace_uses_singleton(singleton, server):
    singleton()
    calls = []

    @guard
    def refund(amount):
        calls.append(amount)
        return "ok"

    assert refund.__name__ == "refund"
    assert refund(5) == "ok" and calls == [5]
    cand = server.bodies("check")[0]["candidate"]
    assert (cand["name"], cand["kind"], cand["input"], cand["attrs"]) == ("refund", "tool", {}, {})
    assert server.bodies("check")[0]["trace_uid"] is None


def test_guard_blocked_does_not_run(singleton, server):
    singleton()
    server.queue("check", BLOCK)
    calls = []

    @guard(name="issue_refund", kind="custom", tier="gold")
    def refund():
        calls.append(1)

    with pytest.raises(PolicyBlocked, match="no-refunds") as ei:
        refund()
    assert calls == [] and ei.value.result.allowed is False
    cand = server.bodies("check")[0]["candidate"]
    assert (cand["name"], cand["kind"], cand["attrs"]) == ("issue_refund", "custom", {"tier": "gold"})


def test_guard_fail_closed(singleton, server):
    singleton(fail_closed=True)
    server.queue("check", httpx.ConnectError("down"))

    @guard
    def tool():
        return 1

    with pytest.raises(PolicyBlocked, match="ConnectError"):
        tool()


def test_guard_requires_init_or_trace(singleton):
    @guard
    def tool():
        return 1

    with pytest.raises(RuntimeError):
        tool()


def test_guard_records_events_in_trace(client, server):
    @guard(kind="retrieval", index="kb")
    def lookup(q):
        return q

    @guard
    def broken():
        raise KeyError("x")

    with client.trace() as t:
        assert lookup("secret") == "secret"
        with pytest.raises(KeyError):
            broken()
    names = [(e["name"], e["kind"], e["status"], e["attrs"], e["input"]) for e in t.events]
    assert names == [("lookup", "retrieval", "success", {"index": "kb"}, {}),
                     ("broken", "tool", "error", {}, {})]
    assert [b["trace_uid"] for b in server.bodies("check")] == [None, None]
    (p,) = client._drain()
    assert [e["seq"] for e in p["events"]] == [0, 1]


def test_guard_blocked_in_trace_records_nothing(client, server):
    server.queue("check", BLOCK)

    @guard
    def tool():
        return 1

    with client.trace() as t:
        with pytest.raises(PolicyBlocked):
            tool()
    assert t.events == []


def test_guard_in_streamed_trace_checks_with_history(client, server):
    @guard
    def tool():
        return 1

    with client.trace(stream=True) as t:
        t.add_event("llm", "plan")
        tool()
        tool()
    checks = server.bodies("check")
    assert [c["trace_uid"] for c in checks] == [t.trace_id] * 2
    assert [c["candidate"]["seq"] for c in checks] == [1, 2]
    assert server.routes() == ["events", "check", "events", "check", "events", "events"]


def test_guard_skips_recording_in_tool_call_trace(client, server):
    @guard
    def tool():
        return 1

    with client.trace() as t:
        t.add_tool_call("legacy")
        assert tool() == 1
    assert t.events == [] and len(t.tool_calls) == 1


def test_async_guard_without_trace(singleton, server):
    singleton()
    server.queue("check", (200, {"allowed": True, "violations": []}), BLOCK)

    @guard
    async def fetch(x):
        return x * 2

    assert fetch.__name__ == "fetch"
    assert asyncio.run(fetch(2)) == 4
    with pytest.raises(PolicyBlocked):
        asyncio.run(fetch(3))
    assert len(server.bodies("check")) == 2


def test_async_guard_with_async_client_trace(aclient, server):
    ran = []

    @guard(name="web")
    async def search():
        ran.append(1)
        return "r"

    @guard
    async def fails():
        raise ValueError("x")

    async def run():
        async with aclient.trace(stream=True) as t:
            assert await search() == "r"
            with pytest.raises(ValueError):
                await fails()
            server.queue("check", BLOCK)
            with pytest.raises(PolicyBlocked):
                await search()
        return t

    t = asyncio.run(run())
    assert ran == [1]
    assert [(e["name"], e["status"]) for e in t.events] == [("web", "success"), ("fails", "error")]
    assert [c["candidate"]["seq"] for c in server.bodies("check")] == [0, 1, 2]
    flat = [s for b in server.bodies("events") for s in (e["seq"] for e in b["events"])]
    assert flat == [0, 1] and server.bodies("events")[-1]["final"] is True


def test_sync_guard_inside_async_trace(client, server):
    @guard
    def tool():
        return 1

    async def run():
        async with client.trace() as t:
            tool()
        return t

    t = asyncio.run(run())
    assert [e["name"] for e in t.events] == ["tool"]
    assert agentops_sdk.guard is guard
