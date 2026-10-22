from datetime import datetime
from typing import Annotated, List, Literal, Optional, Dict, Any, Union
from pydantic import BaseModel, Field, field_validator

from app.services import cron


class WebhookRunner(BaseModel):
    """POST each case to `url`; the 2xx JSON body's `output` string is the case's output."""
    type: Literal["webhook"]
    url: str = Field(min_length=1, max_length=2048, pattern=r"^https?://")
    timeout_s: float = Field(default=10.0, gt=0, le=60)


class TracesRunner(BaseModel):
    """The output of the newest trace of the run's agent whose input matches the case's."""
    type: Literal["traces"]
    match: Literal["exact", "contains"] = "exact"
    lookback_days: int = Field(default=7, ge=1, le=90)


EvalRunner = Annotated[Union[WebhookRunner, TracesRunner], Field(discriminator="type")]


class CaseIn(BaseModel):
    input: str
    expected_output: str = ""
    tags: List[str] = []
    weight: float = 1.0


def _valid_cron(v: Optional[str]) -> Optional[str]:
    if v is not None:
        cron.parse(v)
    return v


class EvalSetCreate(BaseModel):
    name: str
    description: str = ""
    evaluators: List[str] = ["exact", "semantic"]
    schedule_cron: Optional[str] = None
    runner: Optional[EvalRunner] = None
    cases: List[CaseIn] = []

    _cron = field_validator("schedule_cron")(_valid_cron)


class EvalSetUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    evaluators: Optional[List[str]] = None
    schedule_cron: Optional[str] = None
    runner: Optional[EvalRunner] = None  # an explicit null removes it

    _cron = field_validator("schedule_cron")(_valid_cron)


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
    runner: Optional[EvalRunner]
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
