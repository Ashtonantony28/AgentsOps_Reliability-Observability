from datetime import datetime
from typing import List, Literal, Optional
from pydantic import BaseModel, Field

from app.schemas.trace import EventIn

PolicySeverity = Literal["info", "low", "medium", "high", "critical"]
PolicyAction = Literal["log", "alert", "block"]
ViolationMode = Literal["batch", "live", "precheck"]


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
    message: str
    mode: ViolationMode
    detected_at: datetime

    class Config:
        from_attributes = True


class PolicyCheckIn(BaseModel):
    agent_id: str
    trace_uid: Optional[str] = None
    candidate: EventIn


class PolicyCheckOut(BaseModel):
    allowed: bool
    violations: List[ViolationOut] = []


class EventsAppendIn(BaseModel):
    agent_id: str
    events: List[EventIn]
    final: bool = False


class EventsAppendOut(BaseModel):
    accepted: int
    violations: List[ViolationOut] = []


class WsEventsMessage(EventsAppendIn):
    type: Literal["events"]
    trace_uid: str = Field(max_length=128)


class ViolationMessage(BaseModel):
    type: Literal["violation"] = "violation"
    data: ViolationOut
