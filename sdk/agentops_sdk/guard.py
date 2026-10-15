from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import logging
from typing import Callable, Optional

from agentops_sdk import client as _client
from agentops_sdk.precheck import PolicyBlocked
from agentops_sdk.trace import current_trace
from agentops_sdk.utils import now_ms

log = logging.getLogger("agentops_sdk")


@contextlib.asynccontextmanager
async def _alock(t):
    if t is None:
        yield
        return
    if t._guard_alock is None:
        t._guard_alock = asyncio.Lock()
    async with t._guard_alock:
        yield


def guard(fn: Optional[Callable] = None, *, name: Optional[str] = None, kind: str = "tool", **attrs):
    """Pre-checks a tool call against trace policies and raises PolicyBlocked instead of running it.

    Inside an active trace the call is checked against that trace and recorded with add_event when it starts, its
    status and latency set when it ends unless the event was sent meanwhile; checking and recording hold a per-trace
    lock, so each check sees the calls started before it. Otherwise it is checked through the client from init().
    Function arguments are never sent.
    """
    def wrap(f: Callable) -> Callable:
        ev_name = name or f.__name__

        def _start(t) -> Optional[dict]:
            if t is not None and t.tool_calls:
                log.warning("agentops guard: trace %s uses add_tool_call; %s not recorded", t.trace_id, ev_name)
            elif t is not None:
                a = dict(attrs)
                return t._add(kind, ev_name, "success", a.pop("input", None), a.pop("output", None), a, running=True)
            return None

        def _end(t, ev: Optional[dict], start: int, status: str) -> None:
            if ev is not None:
                t._end(ev, status, now_ms() - start)

        if inspect.iscoroutinefunction(f):
            @functools.wraps(f)
            async def awrapper(*args, **kwargs):
                t = current_trace()
                async with _alock(t):
                    result = await (t.acheck if t else _client.acheck)(ev_name, kind=kind, **attrs)
                    if not result.allowed:
                        raise PolicyBlocked(result)
                    ev, start = _start(t), now_ms()
                try:
                    out = await f(*args, **kwargs)
                except BaseException:
                    _end(t, ev, start, "error")
                    raise
                _end(t, ev, start, "success")
                return out
            return awrapper

        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            t = current_trace()
            with t._guard_lock if t is not None else contextlib.nullcontext():
                result = (t.check if t else _client.check)(ev_name, kind=kind, **attrs)
                if not result.allowed:
                    raise PolicyBlocked(result)
                ev, start = _start(t), now_ms()
            try:
                out = f(*args, **kwargs)
            except BaseException:
                _end(t, ev, start, "error")
                raise
            _end(t, ev, start, "success")
            return out
        return wrapper

    return wrap(fn) if fn is not None else wrap
