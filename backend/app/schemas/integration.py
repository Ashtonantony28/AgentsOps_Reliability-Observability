from datetime import datetime
from typing import Dict, Any, Optional
from pydantic import BaseModel


class IntegrationCreate(BaseModel):
    kind: str
    name: str
    config: Dict[str, Any] = {}


class IntegrationUpdate(BaseModel):
    name: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = None


class IntegrationOut(BaseModel):
    id: str
    org_id: str
    kind: str
    name: str
    config: Dict[str, Any]
    enabled: bool
    last_sync_at: Optional[datetime]
    last_status: str
    error_count: int
    last_error: str
    created_at: datetime

    class Config:
        from_attributes = True


class TestConnectionResult(BaseModel):
    ok: bool
    message: str
    details: Dict[str, Any] = {}


class PolicyRuleCreate(BaseModel):
    name: str
    description: str = ""
    rule: Dict[str, Any]
    action: str = "block"  # block|warn|log
    enabled: bool = True


class PolicyRuleOut(BaseModel):
    id: str
    org_id: str
    name: str
    description: str
    rule: Dict[str, Any]
    action: str
    enabled: bool
    created_at: datetime

    class Config:
        from_attributes = True


class AuditLogOut(BaseModel):
    id: str
    org_id: str
    actor_type: str
    actor_id: Optional[str]
    action: str
    resource_type: str
    resource_id: Optional[str]
    rationale: str
    details: Dict[str, Any]
    created_at: datetime

    class Config:
        from_attributes = True
