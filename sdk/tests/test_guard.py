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


# A6-7, A8-3: guarded calls in one trace are checked one at a time, each after the ones started before it are
# recorded; the calls themselves overlap
@pytest.mark.parametrize("stream", [False, True])
def test_concurrent_async_guarded_calls_are_checked_in_turn(client, aclient, server, stream):
    for c in (client, aclient):
        server.requests.clear()
        ran = []

        @guard
        async def delete_user(uid):
            ran.append(("start", uid))
            await asyncio.sleep(0.01)
            ran.append(("end", uid))

        async def main():
            async with c.trace(stream=stream):
                await asyncio.gather(delete_user(1), delete_user(2))

        asyncio.run(main())
        assert ran == [("start", 1), ("start", 2), ("end", 1), ("end", 2)]
        first, second = server.bodies("check")
        if stream:
            routes = server.routes()
            assert routes.index("events") < len(routes) - 1 - routes[::-1].index("check")
            assert server.bodies("events")[0]["events"][0]["name"] == "delete_user"
        else:
            assert (first["prefix"], [e["name"] for e in second["prefix"]]) == ([], ["delete_user"])


def test_concurrent_sync_guarded_calls_are_checked_in_turn(client, server):
    import contextvars
    import threading
    import time

    @guard
    def delete_user():
        time.sleep(0.02)

    with client.trace():
        threads = [threading.Thread(target=contextvars.copy_context().run, args=(delete_user,)) for _ in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
    assert sorted(len(b["prefix"]) for b in server.bodies("check")) == [0, 1]


def test_nested_guarded_calls_do_not_deadlock(client, server):
    @guard
    def inner():
        return 1

    @guard
    def outer():
        return inner() + 1

    @guard
    async def ainner():
        return 1

    @guard
    async def aouter():
        return await ainner() + 1

    async def amain():
        async with client.trace() as t:
            assert await asyncio.wait_for(aouter(), 1) == 2
        return t

    with client.trace() as t:
        assert outer() == 2
    assert [e["name"] for e in t.events] == ["outer", "inner"]
    assert [e["name"] for e in asyncio.run(amain()).events] == ["aouter", "ainner"]


# A8-3: a call's event is recorded when it starts; its status and latency are set when it ends unless it was sent
@pytest.mark.parametrize("stream", [False, True])
def test_guarded_call_outcome_is_set_when_it_ends(aclient, server, stream):
    @guard
    async def slow(fail, s=0.02):
        await asyncio.sleep(s)
        if fail:
            raise ValueError("x")

    async def main():
        async with aclient.trace(stream=stream) as t:
            with pytest.raises(ValueError):
                await slow(True)
            await asyncio.gather(slow(False), slow(True, 0.1), return_exceptions=True)
        return t

    t = asyncio.run(main())
    got = [(e["status"], e["latency_ms"] >= 15) for e in t.events]
    if stream:  # the second gathered check sent the first's event, and the first's end push the second's
        assert got == [("error", True), ("success", False), ("success", False)]
        assert [e["status"] for b in server.bodies("events") for e in b["events"]] == ["error", "success", "success"]
    else:
        assert got == [("error", True), ("success", True), ("error", True)]


def test_sync_guarded_calls_in_threads_overlap_and_check_in_turn(client, server):
    import contextvars
    import threading
    import time

    started = threading.Barrier(2, timeout=2)

    @guard
    def call():
        started.wait()  # both run at once: neither holds the lock across the call
        time.sleep(0.01)

    with client.trace() as t:
        threads = [threading.Thread(target=contextvars.copy_context().run, args=(call,)) for _ in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
    assert sorted(len(b["prefix"]) for b in server.bodies("check")) == [0, 1]
    assert [(e["status"], e["latency_ms"] >= 10) for e in t.events] == [("success", True)] * 2


def test_guarded_calls_in_different_traces_run_concurrently(client, server):
    ran = []

    @guard
    async def slow(i):
        ran.append(("start", i))
        await asyncio.sleep(0.01)
        ran.append(("end", i))

    async def one(i):
        async with client.trace():
            await slow(i)

    async def main():
        await asyncio.gather(one(1), one(2))

    asyncio.run(main())
    assert ran[:2] == [("start", 1), ("start", 2)]
