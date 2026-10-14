from __future__ import annotations

import asyncio
import logging
import threading
from contextvars import ContextVar
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from agentops_sdk.precheck import CheckResult, check_request, event_dict, events_request
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
        if self._streaming() and not self._client.is_async:
            self._push()
        return self._require_client()._post_check(self._check_body(name, kind, input, attrs))

    async def acheck(self, name: str, *, kind: str = "tool", input: Optional[dict] = None, **attrs) -> CheckResult:
        if self._streaming():
            await (self._apush() if self._client.is_async else asyncio.to_thread(self._push))
        return await self._require_client()._apost_check(self._check_body(name, kind, input, attrs))

    def _require_client(self):
        if self._client is None:
            raise RuntimeError("trace has no client to check against")
        return self._client

    def _check_body(self, name: str, kind: str, input: Optional[dict], attrs: dict) -> dict:
        # Without streaming the server has none of this trace's events, so the check has no history.
        if self._streaming():
            return check_request(self.agent_id, self.trace_id,
                                 event_dict(len(self.events), kind, name, input=input, attrs=attrs))
        return check_request(self.agent_id, None, event_dict(0, kind, name, input=input, attrs=attrs))

    def _streaming(self) -> bool:
        return self.stream and self._client is not None

    def _batch(self, final: bool) -> Optional[tuple]:
        if self._dead:
            return None
        pending = self.events[self._sent:]
        if not pending and not final:
            return None
        return pending, events_request(self.agent_id, pending, final)

    def _settle(self, outcome: str, n: int, final: bool) -> None:
        if outcome == "ok":
            self._sent += n
            return
        if outcome == "fatal":
            self._dead = True
            log.warning("agentops stopped streaming trace %s; dropped %d events", self.trace_id, n)
        elif final:
            log.warning("agentops final push failed for trace %s; %d events not delivered", self.trace_id, n)

    def _push(self, final: bool = False) -> None:
        with self._lock:
            batch = self._batch(final)
            if batch is not None:
                self._settle(self._client._send_events(self.trace_id, batch[1]), len(batch[0]), final)

    async def _apush(self, final: bool = False) -> None:
        if self._alock is None:
            self._alock = asyncio.Lock()
        async with self._alock:
            batch = self._batch(final)
            if batch is not None:
                self._settle(await self._client._asend_events(self.trace_id, batch[1]), len(batch[0]), final)

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
        return self._streaming() and bool(self.events)

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
