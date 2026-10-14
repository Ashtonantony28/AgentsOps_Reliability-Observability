from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field

from app.schemas.trace import INT32_MAX, MAX_TRACE_EVENTS, EventIn

PolicySeverity = Literal["info", "low", "medium", "high", "critical"]
PolicyAction = Literal["log", "alert", "block"]
ViolationMode = Literal["batch", "live", "precheck"]
# pending: a live row the batch check has not reached; fallback: sufficient but maybe not minimal (the solver ran
# out of budget or failed); none: never computed (a precheck, or a live row the batch check did not find)
CounterexampleStatus = Literal["pending", "minimal", "fallback", "none"]


class TracePolicyCreate(BaseModel):
    source: str                      # exactly one `policy` block; name and severity come from it
    action: PolicyAction = "log"
    agent_id: Optional[str] = None
    environment: Optional[str] = None
    enabled: bool = True


class TracePolicyUpdate(BaseModel):
    source: Optional[str] = None
    action: Optional[PolicyAction] = None
    agent_id: Optional[str] = None
    environment: Optional[str] = None
    enabled: Optional[bool] = None


class TracePolicyOut(BaseModel):
    id: str
    org_id: str
    name: str
    source: str
    severity: PolicySeverity
    action: PolicyAction
    agent_id: Optional[str]
    environment: Optional[str]
    enabled: bool
    version: int
    created_at: datetime
    updated_at: datetime
    archived_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class PolicyValidateIn(BaseModel):
    source: str


class PolicyError(BaseModel):
    line: int
    col: int
    message: str


class PolicyValidateOut(BaseModel):
    ok: bool
    errors: List[PolicyError] = []


class ViolationOut(BaseModel):
    id: Optional[str] = None         # None for precheck results, which are not stored
    trace_id: Optional[str] = None
    agent_id: str
    policy_id: str
    policy_name: str
    policy_version: int
    severity: PolicySeverity
    at_seq: int
    counterexample: List[int]
    counterexample_status: CounterexampleStatus
    message: str
    mode: ViolationMode
    detected_at: datetime

    class Config:
        from_attributes = True


class PolicyCheckIn(BaseModel):
    agent_id: str
    trace_uid: Optional[str] = None
    environment: Optional[str] = Field(None, max_length=32)  # used when the trace is not stored yet
    # events the server does not hold, after any it holds for trace_uid; seqs ignored, like the candidate's
    prefix: List[EventIn] = Field(default_factory=list, max_length=MAX_TRACE_EVENTS)
    candidate: EventIn


class PolicyCheckOut(BaseModel):
    allowed: bool
    violations: List[ViolationOut] = []


class EventsAppendIn(BaseModel):
    """Trace-level fields mean "unchanged" when null; `environment` is fixed by the append that creates the trace."""
    agent_id: str
    events: List[EventIn] = Field(max_length=MAX_TRACE_EVENTS)
    final: bool = False
    environment: Optional[str] = Field(None, max_length=32)
    agent_version: Optional[str] = Field(None, max_length=64)
    session_id: Optional[str] = Field(None, max_length=128)
    input: Optional[str] = None
    output: Optional[str] = None
    status: Optional[str] = Field(None, max_length=32)
    error_message: Optional[str] = None
    latency_ms: Optional[int] = Field(None, le=INT32_MAX)
    tokens_input: Optional[int] = Field(None, le=INT32_MAX)
    tokens_output: Optional[int] = Field(None, le=INT32_MAX)
    cost_usd: Optional[float] = None
    metadata: Optional[Dict[str, Any]] = None


class EventsAppendOut(BaseModel):
    accepted: int
    violations: List[ViolationOut] = []


class WsEventsMessage(EventsAppendIn):
    type: Literal["events"]
    trace_uid: str = Field(max_length=128)


class ViolationMessage(BaseModel):
    type: Literal["violation"] = "violation"
    data: ViolationOut
