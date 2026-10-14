from datetime import datetime, timedelta
from typing import Optional, get_args

import numpy as np
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AlertEvent, AlertRule, Incident, Trace, TracePolicyViolation
from app.schemas.policy import PolicySeverity

SEVERITIES = get_args(PolicySeverity)  # ascending


def severities_at_least(rule: AlertRule) -> tuple[str, ...]:
    floor = (rule.context or {}).get("severity_at_least")
    return SEVERITIES[SEVERITIES.index(floor):] if floor in SEVERITIES else SEVERITIES


def policy_violations_query(rule: AlertRule, since: datetime):
    q = select(func.count()).select_from(TracePolicyViolation).where(
        TracePolicyViolation.org_id == rule.org_id, TracePolicyViolation.detected_at >= since,
        TracePolicyViolation.severity.in_(severities_at_least(rule)))
    return q.where(TracePolicyViolation.agent_id == rule.agent_id) if rule.agent_id else q


async def compute_metric_value(db: AsyncSession, rule: AlertRule) -> float:
    window = datetime.utcnow() - timedelta(minutes=rule.window_minutes or 15)
    if rule.metric == "policy_violations":
        return float((await db.execute(policy_violations_query(rule, window))).scalar_one())
    q = select(Trace).where(Trace.created_at >= window, Trace.org_id == rule.org_id)
    if rule.agent_id:
        q = q.where(Trace.agent_id == rule.agent_id)
    rows = (await db.execute(q)).scalars().all()
    if not rows:
        return 0.0
    if rule.metric == "error_rate":
        errors = sum(1 for r in rows if r.status != "success")
        return round(errors / len(rows), 4)
    if rule.metric == "latency_p95":
        return float(np.percentile([r.latency_ms for r in rows], 95))
    if rule.metric == "latency_avg":
        return float(np.mean([r.latency_ms for r in rows]))
    if rule.metric == "cost_per_day":
        day = datetime.utcnow() - timedelta(days=1)
        cost_rows = [r.cost_usd for r in rows if r.created_at >= day]
        return float(sum(cost_rows))
    if rule.metric == "token_usage":
        return float(sum((r.tokens_input + r.tokens_output) for r in rows))
    return 0.0


def compare(value: float, op: str, threshold: float) -> bool:
    return {
        ">": value > threshold,
        ">=": value >= threshold,
        "<": value < threshold,
        "<=": value <= threshold,
        "==": value == threshold,
    }.get(op, False)


async def already_firing(db: AsyncSession, rule: AlertRule) -> bool:
    dedup = datetime.utcnow() - timedelta(minutes=rule.dedup_minutes or 30)
    existing = (await db.execute(
        select(AlertEvent).where(
            AlertEvent.rule_id == rule.id,
            AlertEvent.status == "firing",
            AlertEvent.fired_at >= dedup,
        ).limit(1)
    )).scalar_one_or_none()
    return existing is not None


async def fire_alert(db: AsyncSession, rule: AlertRule, value: float, context: Optional[dict] = None,
                     incident: bool = False) -> AlertEvent:
    event = AlertEvent(
        rule_id=rule.id,
        org_id=rule.org_id,
        agent_id=rule.agent_id,
        value=value,
        message=f"{rule.metric} {rule.operator} {rule.threshold} (observed {value})",
        context={"window_minutes": rule.window_minutes, "metric": rule.metric, **(context or {})},
    )
    db.add(event)

    # Auto-create incident for critical
    if incident or rule.severity == "critical":
        inc = Incident(
            org_id=rule.org_id,
            agent_id=rule.agent_id,
            title=f"{rule.name}: {rule.metric} breached",
            severity=rule.severity,
            timeline=[{
                "ts": datetime.utcnow().isoformat(),
                "event": "alert_fired",
                "detail": event.message,
            }],
        )
        db.add(inc)
    await db.commit()
    return event


async def build_incident_narrative(db: AsyncSession, incident: Incident) -> str:
    window_start = incident.started_at - timedelta(minutes=15)
    rows = (await db.execute(
        select(Trace).where(
            Trace.org_id == incident.org_id,
            Trace.created_at >= window_start,
            Trace.created_at <= (incident.resolved_at or datetime.utcnow()),
        ).order_by(Trace.created_at.desc()).limit(200)
    )).scalars().all()
    errors = [r for r in rows if r.status != "success"]
    total = len(rows)
    err_rate = (len(errors) / total) if total else 0.0
    sample_err = errors[0] if errors else None
    parts = [
        f"Incident '{incident.title}' opened at {incident.started_at.isoformat()}.",
        f"Window saw {total} traces, {len(errors)} errors ({err_rate:.1%}).",
    ]
    if sample_err and sample_err.error_message:
        parts.append(f"Sample error: {sample_err.error_message[:200]}")
    return " ".join(parts)
