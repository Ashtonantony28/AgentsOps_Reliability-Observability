from datetime import datetime
from typing import Optional, List, Dict, Any, Literal
from pydantic import BaseModel, Field, model_validator


class ToolCallIn(BaseModel):
    name: str
    input: Dict[str, Any] = {}
    output: Dict[str, Any] = {}
    latency_ms: int = 0
    status: str = "success"


class EventIn(BaseModel):
    seq: int = Field(ge=0)
    kind: Literal["tool", "llm", "retrieval", "confirmation", "custom"] = "tool"
    name: str = Field(min_length=1)
    status: str = "success"
    input: Dict[str, Any] = {}
    output: Dict[str, Any] = {}
    attrs: Dict[str, Any] = {}
    latency_ms: int = 0
    timestamp: Optional[datetime] = None


class TraceIn(BaseModel):
    trace_id: Optional[str] = None
    agent_id: str
    session_id: Optional[str] = None
    input: str = ""
    output: str = ""
    status: str = "success"
    latency_ms: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    cost_usd: float = 0.0
    environment: str = "dev"
    agent_version: str = "1.0.0"
    error_message: Optional[str] = None
    tool_calls: List[ToolCallIn] = []
    events: List[EventIn] = []
    metadata: Dict[str, Any] = Field(default_factory=dict)
    timestamp: Optional[datetime] = None

    @model_validator(mode="after")
    def _check_events(self):
        if self.events and self.tool_calls:
            raise ValueError("send either events or tool_calls, not both")
        if [e.seq for e in self.events] != list(range(len(self.events))):
            raise ValueError("event seq must be 0, 1, 2, ... in order")
        return self


class TraceBulkIn(BaseModel):
    traces: List[TraceIn]


class SpanOut(BaseModel):
    id: str
    trace_id: str
    parent_span_id: Optional[str]
    seq: Optional[int] = None
    kind: str
    name: str
    input: Dict[str, Any]
    output: Dict[str, Any]
    attrs: Optional[Dict[str, Any]] = None
    latency_ms: int
    status: str
    started_at: datetime
    ended_at: Optional[datetime]

    class Config:
        from_attributes = True


class TraceOut(BaseModel):
    id: str
    org_id: str
    agent_id: str
    session_id: Optional[str]
    trace_uid: str
    status: str
    input: str
    output: str
    error_message: Optional[str]
    latency_ms: int
    tokens_input: int
    tokens_output: int
    cost_usd: float
    environment: str
    agent_version: str
    pii_flags: List[str]
    injection_score: float
    meta: Dict[str, Any]
    created_at: datetime

    class Config:
        from_attributes = True


class TraceDetailOut(TraceOut):
    spans: List[SpanOut] = []


class TimeSeriesPoint(BaseModel):
    t: datetime
    value: float


class MetricsResponse(BaseModel):
    success_rate: List[TimeSeriesPoint]
    error_rate: List[TimeSeriesPoint]
    latency_avg_ms: List[TimeSeriesPoint]
    latency_p95_ms: List[TimeSeriesPoint]
    tokens: List[TimeSeriesPoint]
    cost_usd: List[TimeSeriesPoint]
    totals: Dict[str, float]


class CostBurn(BaseModel):
    daily: List[TimeSeriesPoint]
    monthly_projection_usd: float
    by_agent: List[Dict[str, Any]]
