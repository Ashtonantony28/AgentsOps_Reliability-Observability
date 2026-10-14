from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from app.models.trace import Span
from app.schemas.trace import EventIn, TraceIn


@dataclass(frozen=True)
class Event:
    seq: int
    kind: str
    name: str
    status: str
    attrs: Mapping[str, Any] = field(default_factory=dict)


def event_inputs(trace: TraceIn) -> list[EventIn]:
    if trace.events:
        return list(trace.events)
    return [
        EventIn.model_construct(seq=i, kind="tool", name=tc.name, status=tc.status, input=tc.input,
                                output=tc.output, attrs={}, latency_ms=tc.latency_ms, timestamp=None)
        for i, tc in enumerate(trace.tool_calls)
    ]


def events_from_trace(trace: TraceIn) -> list[Event]:
    return [Event(e.seq, e.kind, e.name, e.status, dict(e.attrs)) for e in event_inputs(trace)]


def events_from_spans(spans: Sequence[Span]) -> list[Event]:
    ordered = sorted(spans, key=lambda s: s.seq)
    return [Event(s.seq, s.kind, s.name, s.status, dict(s.attrs or {})) for s in ordered]
