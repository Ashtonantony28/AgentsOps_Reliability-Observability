from datetime import datetime
from typing import List, Optional, Dict, Any
from pydantic import BaseModel

from app.schemas.policy import PolicySeverity


class AlertChannel(BaseModel):
    type: str  # email|slack|pagerduty|webhook
    target: str  # email address or url
    name: str = ""


class AlertRuleContext(BaseModel):
    severity_at_least: Optional[PolicySeverity] = None  # policy_violations: count only this severity and above


class AlertRuleCreate(BaseModel):
    name: str
    agent_id: Optional[str] = None
    metric: str
    operator: str = ">"
    threshold: float
    window_minutes: int = 15
    severity: str = "warning"
    channels: List[AlertChannel] = []
    runbook_url: str = ""
    dedup_minutes: int = 30
    enabled: bool = True
    context: AlertRuleContext = AlertRuleContext()


class AlertRuleUpdate(BaseModel):
    name: Optional[str] = None
    threshold: Optional[float] = None
    operator: Optional[str] = None
    window_minutes: Optional[int] = None
    severity: Optional[str] = None
    channels: Optional[List[AlertChannel]] = None
    runbook_url: Optional[str] = None
    dedup_minutes: Optional[int] = None
    enabled: Optional[bool] = None
    context: Optional[AlertRuleContext] = None


class AlertRuleOut(BaseModel):
    id: str
    org_id: str
    agent_id: Optional[str]
    name: str
    metric: str
    operator: str
    threshold: float
    window_minutes: int
    severity: str
    channels: List[Dict[str, Any]]
    runbook_url: str
    dedup_minutes: int
    enabled: bool
    context: Dict[str, Any] = {}
    created_at: datetime

    class Config:
        from_attributes = True


class AlertEventOut(BaseModel):
    id: str
    rule_id: str
    org_id: str
    agent_id: Optional[str]
    status: str
    value: float
    message: str
    assignee_id: Optional[str]
    fired_at: datetime
    acknowledged_at: Optional[datetime]
    resolved_at: Optional[datetime]
    context: Dict[str, Any]

    class Config:
        from_attributes = True


class IncidentOut(BaseModel):
    id: str
    org_id: str
    agent_id: Optional[str]
    title: str
    status: str
    severity: str
    timeline: List[Dict[str, Any]]
    narrative: str
    started_at: datetime
    resolved_at: Optional[datetime]

    class Config:
        from_attributes = True
