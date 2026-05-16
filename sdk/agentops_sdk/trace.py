from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from agentops_sdk.utils import gen_uid, now_ms


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
        self.tool_calls.append(ToolCall(
            name=name, input=input or {}, output=output or {},
            latency_ms=latency_ms, status=status,
        ))

    def mark_error(self, message: str):
        self.status = "error"
        self.error_message = (message or "")[:1000]

    def to_payload(self) -> dict:
        return {
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

    # Context manager
    def __enter__(self) -> "Trace":
        self._start_ms = now_ms()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.latency_ms = max(self.latency_ms, now_ms() - self._start_ms)
        if exc is not None:
            self.mark_error(f"{exc_type.__name__}: {exc}")
        if self._client is not None:
            self._client.enqueue(self.to_payload())
        return False  # propagate exception

    async def __aenter__(self) -> "Trace":
        self._start_ms = now_ms()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.latency_ms = max(self.latency_ms, now_ms() - self._start_ms)
        if exc is not None:
            self.mark_error(f"{exc_type.__name__}: {exc}")
        if self._client is not None:
            await self._client.aenqueue(self.to_payload())
        return False
