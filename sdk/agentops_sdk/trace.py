from __future__ import annotations

import asyncio
import logging
import threading
from contextvars import ContextVar
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from agentops_sdk.precheck import CheckResult, check_request, event_dict, events_request, failed_result
from agentops_sdk.utils import gen_uid, now_ms

log = logging.getLogger("agentops_sdk")

_current: ContextVar[Optional["Trace"]] = ContextVar("agentops_current_trace", default=None)


def current_trace() -> Optional["Trace"]:
    return _current.get()


@dataclass
class ToolCall:
    name: str
    input: Dict[str, Any] = field(default_factory=dict)
    output: Dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0
    status: str = "success"


@dataclass
class Trace:
    agent_id: str
    environment: str = "dev"
    agent_version: str = "1.0.0"
    trace_id: str = field(default_factory=gen_uid)
    session_id: Optional[str] = None
    input: str = ""
    output: str = ""
    status: str = "success"
    latency_ms: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    cost_usd: float = 0.0
    error_message: Optional[str] = None
    tool_calls: List[ToolCall] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    _start_ms: int = field(default_factory=now_ms)
    _client: Any = field(default=None, repr=False)
    events: List[Dict[str, Any]] = field(default_factory=list)
    stream: bool = False
    _sent: int = field(default=0, init=False, repr=False)
    _dead: bool = field(default=False, init=False, repr=False)
    _split: int = field(default=0, init=False, repr=False)  # events before this index are pushed one at a time
    _refused: int = field(default=0, init=False, repr=False)
    _lock: Any = field(default_factory=threading.Lock, init=False, repr=False, compare=False)
    _alock: Any = field(default=None, init=False, repr=False, compare=False)
    _tasks: set = field(default_factory=set, init=False, repr=False, compare=False)
    _token: Any = field(default=None, init=False, repr=False, compare=False)

    def set_output(self, output: str, *, tokens_input: int = 0, tokens_output: int = 0, cost_usd: float = 0.0):
        self.output = output
        if tokens_input:
            self.tokens_input = tokens_input
        if tokens_output:
            self.tokens_output = tokens_output
        if cost_usd:
            self.cost_usd = cost_usd

    def set_metadata(self, **kwargs):
        self.metadata.update(kwargs)

    def add_tool_call(self, name: str, *, input: Optional[dict] = None, output: Optional[dict] = None,
                      latency_ms: int = 0, status: str = "success"):
        if self.events:
            raise ValueError("a trace holds either events or tool_calls, not both")
        self.tool_calls.append(ToolCall(
            name=name, input=input or {}, output=output or {},
            latency_ms=latency_ms, status=status,
        ))

    def add_event(self, kind: str, name: str, *, status: str = "success", input: Optional[dict] = None,
                  output: Optional[dict] = None, **attrs):
        if self.tool_calls:
            raise ValueError("a trace holds either events or tool_calls, not both")
        latency_ms = attrs.pop("latency_ms", 0)
        self.events.append(event_dict(len(self.events), kind, name, status, input, output, attrs,
                                      latency_ms=latency_ms, timestamp=datetime.now(timezone.utc).isoformat()))
        if not self._streaming():
            return
        if not self._client.is_async:
            self._push()
            return
        try:
            task = asyncio.get_running_loop().create_task(self._apush())
        except RuntimeError:
            return  # no loop: pushed by the next acheck or on exit
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def check(self, name: str, *, kind: str = "tool", input: Optional[dict] = None, **attrs) -> CheckResult:
        if self._streaming():
            self._push()  # sync even with an async client: a sync check can't await the scheduled push
        return self._undelivered() or self._require_client()._post_check(self._check_body(name, kind, input, attrs))

    async def acheck(self, name: str, *, kind: str = "tool", input: Optional[dict] = None, **attrs) -> CheckResult:
        if self._streaming():
            await (self._apush() if self._client.is_async else asyncio.to_thread(self._push))
        return self._undelivered() or await self._require_client()._apost_check(
            self._check_body(name, kind, input, attrs))

    def _undelivered(self) -> Optional[CheckResult]:
        """A failed check when the server lacks some of this streamed trace's events. Once it rejected some, a
        fail-open client checks against the events the server holds instead."""
        if self._streaming() and self._sent < len(self.events) and (not self._dead or self._client.fail_closed):
            return failed_result("trace events not delivered", self._client.fail_closed)
        return None

    def _require_client(self):
        if self._client is None:
            raise RuntimeError("trace has no client to check against")
        return self._client

    def _check_body(self, name: str, kind: str, input: Optional[dict], attrs: dict) -> dict:
        if self._streaming():
            return check_request(self.agent_id, self.trace_id,
                                 event_dict(len(self.events), kind, name, input=input, attrs=attrs))
        # the server holds none of a non-streamed trace's events: they go along as the prefix, without payloads
        prefix = ([event_dict(e["seq"], e["kind"], e["name"], e["status"], attrs=e["attrs"]) for e in self.events]
                  or [event_dict(i, "tool", tc.name, tc.status) for i, tc in enumerate(self.tool_calls)])
        return check_request(self.agent_id, None, event_dict(len(prefix), kind, name, input=input, attrs=attrs),
                             prefix)

    def _streaming(self) -> bool:
        return self.stream and self._client is not None

    def _batch(self, final: bool) -> Optional[tuple]:
        if self._dead:
            return None
        start = self._sent
        pending = self.events[start:start + 1] if start < self._split else self.events[start:]
        if not pending and not final:
            return None
        last = start + len(pending) == len(self.events)
        return start, pending, events_request(self.agent_id, pending, final and last)

    def _settle(self, outcome, start: int, sent: list, final: bool) -> bool:
        """Applies the outcome of pushing `sent`, which was events[start:start + len(sent)]; True when another
        batch should go now. A sync and an async push of the same trace may overlap."""
        n = len(sent)
        if outcome == "ok":
            self._sent = max(self._sent, start + n)
            return self._sent < len(self.events) and (self._sent < self._split or final)
        if isinstance(outcome, int) and start < outcome <= len(self.events):
            self._sent = max(self._sent, outcome)
            return True
        if outcome == "retry":
            if final:
                log.warning("agentops final push failed for trace %s; %d events not delivered", self.trace_id, n)
            return False
        if outcome == "refused":
            if not n:  # a legacy rule refused the empty final push: nothing to drop
                return False
            if any(a is not b for a, b in zip(self.events[start:start + n], sent)) or len(self.events) < start + n:
                return True  # an overlapping push already settled this batch
            if n > 1:  # find the refused event: the ones before it are allowed
                self._split = start + n
                return True
            del self.events[start]
            for i in range(start, len(self.events)):
                self.events[i]["seq"] = i
            self._split = max(start, self._split - 1)
            self._refused += 1
            return final or start < len(self.events)
        self._dead = True
        log.warning("agentops stopped streaming trace %s; dropped %d events", self.trace_id, n)
        return False

    def _push(self, final: bool = False) -> None:
        with self._lock:
            for _ in range(2 * (len(self.events) - self._sent) + 2):
                batch = self._batch(final)
                if batch is None or not self._settle(self._client._send_events(self.trace_id, batch[2]),
                                                     *batch[:2], final):
                    return

    async def _apush(self, final: bool = False) -> None:
        if self._alock is None:
            self._alock = asyncio.Lock()
        async with self._alock:
            for _ in range(2 * (len(self.events) - self._sent) + 2):
                batch = self._batch(final)
                if batch is None or not self._settle(await self._client._asend_events(self.trace_id, batch[2]),
                                                     *batch[:2], final):
                    return

    def mark_error(self, message: str):
        self.status = "error"
        self.error_message = (message or "")[:1000]

    def to_payload(self) -> dict:
        payload = {
            "trace_id": self.trace_id,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "input": self.input,
            "output": self.output,
            "status": self.status,
            "latency_ms": self.latency_ms or max(0, now_ms() - self._start_ms),
            "tokens_input": self.tokens_input,
            "tokens_output": self.tokens_output,
            "cost_usd": self.cost_usd,
            "environment": self.environment,
            "agent_version": self.agent_version,
            "error_message": self.error_message,
            "tool_calls": [asdict(tc) for tc in self.tool_calls],
            "metadata": self.metadata,
            "timestamp": datetime.utcnow().isoformat(),
        }
        if self.events:
            payload["events"] = list(self.events)
        return payload

    # Context manager
    def _enter(self) -> "Trace":
        self._start_ms = now_ms()
        self._token = _current.set(self)
        return self

    def _finish(self, exc_type, exc) -> bool:
        """Records latency/error and resets the current trace; True when the events were streamed."""
        self.latency_ms = max(self.latency_ms, now_ms() - self._start_ms)
        if exc is not None:
            self.mark_error(f"{exc_type.__name__}: {exc}")
        if self._token is not None:
            try:
                _current.reset(self._token)
            except ValueError:
                pass
            self._token = None
        return self._streaming() and bool(self.events or self._refused)

    def __enter__(self) -> "Trace":
        return self._enter()

    def __exit__(self, exc_type, exc, tb):
        if self._finish(exc_type, exc):
            self._push(final=True)
        elif self._client is not None:
            self._client.enqueue(self.to_payload())
        return False  # propagate exception

    async def __aenter__(self) -> "Trace":
        return self._enter()

    async def __aexit__(self, exc_type, exc, tb):
        if self._finish(exc_type, exc):
            if self._client.is_async:
                await self._apush(final=True)
                await asyncio.gather(*self._tasks, return_exceptions=True)
            else:
                await asyncio.to_thread(self._push, True)
        elif self._client is not None:
            await self._client.aenqueue(self.to_payload())
        return False
