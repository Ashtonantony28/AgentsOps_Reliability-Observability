from __future__ import annotations

import functools
import inspect
import logging
from typing import Callable, Optional

from agentops_sdk import client as _client
from agentops_sdk.precheck import PolicyBlocked
from agentops_sdk.trace import current_trace
from agentops_sdk.utils import now_ms

log = logging.getLogger("agentops_sdk")


def guard(fn: Optional[Callable] = None, *, name: Optional[str] = None, kind: str = "tool", **attrs):
    """Pre-checks a tool call against trace policies and raises PolicyBlocked instead of running it.

    Inside an active trace the call is checked against that trace and recorded with add_event;
    otherwise it is checked through the client from init(). Function arguments are never sent.
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
