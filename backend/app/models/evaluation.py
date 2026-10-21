from datetime import datetime
import uuid
from sqlalchemy import String, DateTime, ForeignKey, Integer, JSON, Float, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class EvaluationSet(Base):
    __tablename__ = "evaluation_sets"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(String, default="")
    evaluators: Mapped[list] = mapped_column(JSON, default=list)  # ["exact","semantic","llm_judge","latency","cost"]
    schedule_cron: Mapped[str | None] = mapped_column(String(64), nullable=True)
    runner: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # EvalRunner; null: no output source
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    cases = relationship("EvaluationCase", back_populates="eval_set", cascade="all, delete-orphan")
    runs = relationship("EvaluationRun", back_populates="eval_set", cascade="all, delete-orphan")


class EvaluationCase(Base):
    __tablename__ = "evaluation_cases"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    set_id: Mapped[str] = mapped_column(ForeignKey("evaluation_sets.id", ondelete="CASCADE"), index=True)
    input: Mapped[str] = mapped_column(String)
    expected_output: Mapped[str] = mapped_column(String, default="")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    weight: Mapped[float] = mapped_column(Float, default=1.0)

    eval_set = relationship("EvaluationSet", back_populates="cases")


class EvaluationRun(Base):
    __tablename__ = "evaluation_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    set_id: Mapped[str] = mapped_column(ForeignKey("evaluation_sets.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    agent_version: Mapped[str] = mapped_column(String(64), default="1.0.0")
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending|running|completed|failed
    score: Mapped[float] = mapped_column(Float, default=0.0)
    passed: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    triggered_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    eval_set = relationship("EvaluationSet", back_populates="runs")
    results = relationship("EvaluationResult", back_populates="run", cascade="all, delete-orphan")


class EvaluationResult(Base):
    __tablename__ = "evaluation_results"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("evaluation_runs.id", ondelete="CASCADE"), index=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("evaluation_cases.id", ondelete="CASCADE"))
    actual_output: Mapped[str] = mapped_column(String, default="")
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    scores_by_evaluator: Mapped[dict] = mapped_column(JSON, default=dict)
    reason: Mapped[str] = mapped_column(String, default="")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)

    run = relationship("EvaluationRun", back_populates="results")
