"""AUDIT-12.md SDK findings as tests; each was xfail(strict=True) until its sub-goal fixed it."""
import asyncio

from agentops_sdk import guard


async def _two(tool):
    return await asyncio.gather(tool(), tool())


# A12-2: the async guard's per-trace asyncio.Lock is created on first use and binds to the loop it first waits in, so a
# sync agent that runs its async tools with asyncio.run per step gets RuntimeError from the second step's guarded calls
def test_guarded_calls_of_one_trace_in_two_event_loops(client, server):
    server.delay = 0.01  # the two calls of a step contend for the lock

    @guard
    async def tool():
        return 1

    with client.trace() as t:
        assert asyncio.run(_two(tool)) == [1, 1] and len(t.events) == 2  # control: the first loop
        assert asyncio.run(_two(tool)) == [1, 1]
    assert len(t.events) == 4
