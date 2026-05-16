from datetime import datetime
from typing import List, Optional, Dict, Any
from pydantic import BaseModel


class CaseIn(BaseModel):
    input: str
    expected_output: str = ""
    tags: List[str] = []
    weight: float = 1.0


class EvalSetCreate(BaseModel):
    name: str
    description: str = ""
    evaluators: List[str] = ["exact", "semantic"]
    schedule_cron: Optional[str] = None
    cases: List[CaseIn] = []


class EvalSetUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    evaluators: Optional[List[str]] = None
    schedule_cron: Optional[str] = None


class CaseOut(BaseModel):
    id: str
    input: str
    expected_output: str
    tags: List[str]
    weight: float

    class Config:
        from_attributes = True


class EvalSetOut(BaseModel):
    id: str
    org_id: str
    name: str
    description: str
    evaluators: List[str]
    schedule_cron: Optional[str]
    created_at: datetime
    case_count: int = 0

    class Config:
        from_attributes = True


class EvalSetDetailOut(EvalSetOut):
    cases: List[CaseOut] = []


class EvalRunRequest(BaseModel):
    agent_id: str
    agent_version: Optional[str] = None


class EvalRunOut(BaseModel):
    id: str
    set_id: str
    agent_id: str
    agent_version: str
    status: str
    score: float
    passed: int
    failed: int
    total: int
    summary: Dict[str, Any]
    triggered_by: Optional[str]
    created_at: datetime
    completed_at: Optional[datetime]

    class Config:
        from_attributes = True


class EvalResultOut(BaseModel):
    id: str
    run_id: str
    case_id: str
    actual_output: str
    passed: bool
    score: float
    scores_by_evaluator: Dict[str, float]
    reason: str
    latency_ms: int

    class Config:
        from_attributes = True


class EvalRunDetailOut(EvalRunOut):
    results: List[EvalResultOut] = []


class EvalDiffRequest(BaseModel):
    run_a_id: str
    run_b_id: str


class EvalDiffOut(BaseModel):
    run_a: EvalRunOut
    run_b: EvalRunOut
    delta_score: float
    regressions: List[Dict[str, Any]]
    improvements: List[Dict[str, Any]]
