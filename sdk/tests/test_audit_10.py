"""AUDIT-10.md SDK findings as failing tests; each is xfail(strict=True) until its sub-goal fixes it."""
import asyncio

from agentops_sdk import guard


# A10-3: background pushes stop short of a running guarded call's event, so while one guarded call runs (an approval,
# a long tool call) nothing added after it is streamed, by any task: no live violations or alerts until it ends
def test_events_after_a_running_guarded_call_are_streamed(aclient, server):
    gate = asyncio.Event()

    @guard
    async def wait_for_approval():
        await gate.wait()

    async def main():
        async with aclient.trace(stream=True) as t:
            call = asyncio.create_task(wait_for_approval())
            while not t._running:
                await asyncio.sleep(0)
            t.add_event("tool", "read_secret")
            t.add_event("tool", "http_post")
            for _ in range(20):
                await asyncio.sleep(0.005)
            streamed = [e["name"] for b in server.bodies("events") for e in b["events"]]
            gate.set()
            await call
        return t, streamed

    t, streamed = asyncio.run(main())
    assert [e["name"] for e in t.events] == ["wait_for_approval", "read_secret", "http_post"]  # control
    assert "read_secret" in streamed


# A10-4: async guarded calls serialise on _guard_alock and sync ones on _guard_lock; a sync guarded call run in a
# thread (asyncio.to_thread, an executor) and an async one in the same trace are checked against the same history
def test_sync_and_async_guarded_calls_are_checked_in_turn(aclient, server):
    server.delay = 0.05  # the async check is on the wire while the sync one is made

    @guard(name="delete_user")
    async def adelete():
        return 1

    @guard(name="delete_user")
    def sdelete():
        return 1

    async def main():
        async with aclient.trace() as t:
            await asyncio.gather(adelete(), asyncio.to_thread(sdelete))
        return t

    t = asyncio.run(main())
    assert [e["name"] for e in t.events] == ["delete_user"] * 2  # control: both ran and were recorded
    assert sorted(len(b["prefix"]) for b in server.bodies("check")) == [0, 1]
