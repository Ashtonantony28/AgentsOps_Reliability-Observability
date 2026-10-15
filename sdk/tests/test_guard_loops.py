import asyncio
import contextvars
import threading
import time
import weakref

from agentops_sdk import guard
from agentops_sdk.trace import loop_lock


def test_loop_lock_is_kept_per_event_loop():
    locks = weakref.WeakKeyDictionary()

    async def get():
        return loop_lock(locks), loop_lock(locks)

    (a, a2), (b, _) = asyncio.run(get()), asyncio.run(get())
    assert a is a2 and a is not b


def test_async_guarded_call_waits_for_a_sync_one_in_another_thread(client, server):
    held = threading.Event()

    @guard
    def sync_tool():
        pass

    @guard
    async def async_tool():
        pass

    with client.trace() as t:
        check = t.check

        def slow_check(name, **kw):  # the sync call holds the trace's guard lock while it is checked
            held.set()
            time.sleep(0.05)
            return check(name, **kw)

        t.check = slow_check
        th = threading.Thread(target=contextvars.copy_context().run, args=(sync_tool,))
        th.start()
        held.wait(1)
        asyncio.run(async_tool())
        th.join()
    assert [e["name"] for e in t.events] == ["sync_tool", "async_tool"]
