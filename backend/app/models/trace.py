from datetime import datetime
import uuid
from sqlalchemy import String, DateTime, ForeignKey, Integer, JSON, Float, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class Trace(Base):
    __tablename__ = "traces"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    trace_uid: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32), default="success", index=True)  # success|error|timeout
    input: Mapped[str] = mapped_column(String, default="")
    output: Mapped[str] = mapped_column(String, default="")
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    tokens_input: Mapped[int] = mapped_column(Integer, default=0)
    tokens_output: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    environment: Mapped[str] = mapped_column(String(32), default="dev", index=True)
    agent_version: Mapped[str] = mapped_column(String(64), default="1.0.0")
    pii_flags: Mapped[list] = mapped_column(JSON, default=list)
    injection_score: Mapped[float] = mapped_column(Float, default=0.0)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    agent = relationship("Agent", back_populates="traces")
    spans = relationship("Span", back_populates="trace", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_trace_agent_created", "agent_id", "created_at"),
        Index("ix_trace_org_created", "org_id", "created_at"),
    )


class Span(Base):
    __tablename__ = "spans"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    trace_id: Mapped[str] = mapped_column(ForeignKey("traces.id", ondelete="CASCADE"), index=True)
    parent_span_id: Mapped[str | None] = mapped_column(String, nullable=True)
    seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str] = mapped_column(String(32), default="tool")  # tool|llm|retrieval|confirmation|custom
    name: Mapped[str] = mapped_column(String(255))
    input: Mapped[dict] = mapped_column(JSON, default=dict)
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    attrs: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="success")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    trace = relationship("Trace", back_populates="spans")

    __table_args__ = (
        Index("ix_span_trace_seq", "trace_id", "seq", unique=True),
    )
