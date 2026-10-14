from datetime import datetime
import uuid
from sqlalchemy import String, DateTime, ForeignKey, Integer, JSON, Boolean, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class TracePolicy(Base):
    __tablename__ = "trace_policies"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String)
    severity: Mapped[str] = mapped_column(String(16), default="medium")  # info|low|medium|high|critical
    action: Mapped[str] = mapped_column(String(16), default="log")  # log|alert|block
    agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), nullable=True)
    environment: Mapped[str | None] = mapped_column(String(32), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_trace_policy_org_name", "org_id", "name", unique=True),
    )


class TracePolicyViolation(Base):
    __tablename__ = "policy_violations"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    trace_id: Mapped[str] = mapped_column(ForeignKey("traces.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"))
    policy_id: Mapped[str] = mapped_column(ForeignKey("trace_policies.id", ondelete="CASCADE"), index=True)
    policy_name: Mapped[str] = mapped_column(String(255))
    policy_version: Mapped[int] = mapped_column(Integer)
    severity: Mapped[str] = mapped_column(String(16))
    at_seq: Mapped[int] = mapped_column(Integer)
    counterexample: Mapped[list] = mapped_column(JSON, default=list)
    message: Mapped[str] = mapped_column(String, default="")
    mode: Mapped[str] = mapped_column(String(16))  # batch|live|precheck
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_violation_org_detected", "org_id", "detected_at"),
        Index("ix_violation_trace_policy_version", "trace_id", "policy_id", "policy_version", unique=True),
    )
