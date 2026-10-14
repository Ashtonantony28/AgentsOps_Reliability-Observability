from datetime import datetime
import uuid
from sqlalchemy import String, DateTime, ForeignKey, Integer, JSON, Float, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    metric: Mapped[str] = mapped_column(String(64))  # error_rate|latency_p95|cost_per_day|eval_score|policy_violations
    operator: Mapped[str] = mapped_column(String(8))  # >|>=|<|<=|==
    threshold: Mapped[float] = mapped_column(Float)
    window_minutes: Mapped[int] = mapped_column(Integer, default=15)
    severity: Mapped[str] = mapped_column(String(16), default="warning")  # info|warning|critical
    channels: Mapped[list] = mapped_column(JSON, default=list)  # [{"type":"slack","url":"..."}, ...]
    runbook_url: Mapped[str] = mapped_column(String, default="")
    dedup_minutes: Mapped[int] = mapped_column(Integer, default=30)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    context: Mapped[dict] = mapped_column(JSON, default=dict)  # policy_violations: {"severity_at_least": ...}
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    events = relationship("AlertEvent", back_populates="rule", cascade="all, delete-orphan")


class AlertEvent(Base):
    __tablename__ = "alert_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    rule_id: Mapped[str] = mapped_column(ForeignKey("alert_rules.id", ondelete="CASCADE"), index=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="firing", index=True)  # firing|acknowledged|resolved
    value: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str] = mapped_column(String, default="")
    assignee_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    fired_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    context: Mapped[dict] = mapped_column(JSON, default=dict)

    rule = relationship("AlertRule", back_populates="events")
