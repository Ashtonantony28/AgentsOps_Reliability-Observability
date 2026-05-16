from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = ""
    model_provider: str = "anthropic"
    model_name: str = "claude-opus-4-7"
    environment: str = "dev"
    tags: List[str] = []
    allowed_tools: List[str] = []
    eval_threshold: float = 0.8


class AgentUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    model_provider: Optional[str] = None
    model_name: Optional[str] = None
    environment: Optional[str] = None
    tags: Optional[List[str]] = None
    allowed_tools: Optional[List[str]] = None
    eval_threshold: Optional[float] = None


class AgentOut(BaseModel):
    id: str
    org_id: str
    name: str
    description: str
    model_provider: str
    model_name: str
    environment: str
    current_version: str
    tags: List[str]
    allowed_tools: List[str]
    eval_threshold: float
    created_at: datetime
    updated_at: datetime
    health: str = "unknown"
    error_rate: float = 0.0
    p95_latency_ms: int = 0
    cost_24h: float = 0.0

    class Config:
        from_attributes = True


class AgentVersionCreate(BaseModel):
    version: str
    model_name: str
    prompt_template: str = ""
    config: dict = {}
    environment: str = "dev"


class AgentVersionOut(BaseModel):
    id: str
    agent_id: str
    version: str
    model_name: str
    prompt_template: str
    config: dict
    environment: str
    created_at: datetime

    class Config:
        from_attributes = True


class PromoteRequest(BaseModel):
    target_environment: str  # staging|prod
    version: str
    force: bool = False  # bypass eval gate


class DependencyEdge(BaseModel):
    parent_id: str
    child_id: str
    call_count: int


class AgentGraph(BaseModel):
    nodes: List[AgentOut]
    edges: List[DependencyEdge]
