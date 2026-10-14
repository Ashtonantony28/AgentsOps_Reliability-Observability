from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import logging
from contextvars import ContextVar
from typing import Callable, Optional

from agentops_sdk import client as _client
from agentops_sdk.precheck import PolicyBlocked
from agentops_sdk.trace import current_trace
from agentops_sdk.utils import now_ms

log = logging.getLogger("agentops_sdk")

_held: ContextVar[frozenset] = ContextVar("agentops_guards_held", default=frozenset())  # ids of traces


@contextlib.asynccontextmanager
async def _aserial(t):
    """One guarded coroutine per trace from check through record; a guarded call nested in another passes."""
    if t is None or id(t) in _held.get():
        yield
        return
    if t._guard_alock is None:
        t._guard_alock = asyncio.Lock()
    async with t._guard_alock:
        token = _held.set(_held.get() | {id(t)})
        try:
            yield
        finally:
            _held.reset(token)


def guard(fn: Optional[Callable] = None, *, name: Optional[str] = None, kind: str = "tool", **attrs):
    """Pre-checks a tool call against trace policies and raises PolicyBlocked instead of running it.

    Inside an active trace the call is checked against that trace and recorded with add_event, and guarded calls
    in one trace run one at a time, so each is checked after the ones before it are recorded; otherwise it is
    checked through the client from init(). Function arguments are never sent.
    """
    def wrap(f: Callable) -> Callable:
        ev_name = name or f.__name__

        def _record(t, start: int, status: str) -> None:
            if t is not None and t.tool_calls:
                log.warning("agentops guard: trace %s uses add_tool_call; %s not recorded", t.trace_id, ev_name)
            elif t is not None:
                t.add_event(kind, ev_name, status=status, latency_ms=now_ms() - start, **attrs)

        if inspect.iscoroutinefunction(f):
            @functools.wraps(f)
            async def awrapper(*args, **kwargs):
                t = current_trace()
                async with _aserial(t):
                    result = await (t.acheck if t else _client.acheck)(ev_name, kind=kind, **attrs)
                    if not result.allowed:
                        raise PolicyBlocked(result)
                    start = now_ms()
                    try:
                        out = await f(*args, **kwargs)
                    except BaseException:
                        _record(t, start, "error")
                        raise
                    _record(t, start, "success")
                    return out
            return awrapper

        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            t = current_trace()
            with t._guard_lock if t is not None else contextlib.nullcontext():
                result = (t.check if t else _client.check)(ev_name, kind=kind, **attrs)
                if not result.allowed:
                    raise PolicyBlocked(result)
                start = now_ms()
                try:
                    out = f(*args, **kwargs)
                except BaseException:
                    _record(t, start, "error")
                    raise
                _record(t, start, "success")
                return out
        return wrapper

    return wrap(fn) if fn is not None else wrap
