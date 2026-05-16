import asyncio
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import select

from app.celery_app import celery
from app.models import AlertEvent, AlertRule, Trace
from app.services.notification_service import dispatch_alert
from app.tasks._sync_db import SyncSession


def _compute(db, rule: AlertRule) -> float:
    window = datetime.utcnow() - timedelta(minutes=rule.window_minutes or 15)
    q = select(Trace).where(Trace.created_at >= window, Trace.org_id == rule.org_id)
    if rule.agent_id:
        q = q.where(Trace.agent_id == rule.agent_id)
    rows = db.execute(q).scalars().all()
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
        return float(sum(r.cost_usd for r in rows if r.created_at >= day))
    if rule.metric == "token_usage":
        return float(sum(r.tokens_input + r.tokens_output for r in rows))
    return 0.0


def _compare(value: float, op: str, threshold: float) -> bool:
    return {
        ">": value > threshold,
        ">=": value >= threshold,
        "<": value < threshold,
        "<=": value <= threshold,
        "==": value == threshold,
    }.get(op, False)


@celery.task(name="app.tasks.alert_tasks.evaluate_alert_rules")
def evaluate_alert_rules() -> dict:
    summary = {"checked": 0, "fired": 0}
    with SyncSession() as db:
        rules = db.execute(select(AlertRule).where(AlertRule.enabled == True)).scalars().all()  # noqa
        for rule in rules:
            summary["checked"] += 1
            value = _compute(db, rule)
            if not _compare(value, rule.operator, rule.threshold):
                continue

            # dedup
            dedup_window = datetime.utcnow() - timedelta(minutes=rule.dedup_minutes or 30)
            existing = db.execute(
                select(AlertEvent).where(
                    AlertEvent.rule_id == rule.id,
                    AlertEvent.status == "firing",
                    AlertEvent.fired_at >= dedup_window,
                ).limit(1)
            ).scalar_one_or_none()
            if existing:
                continue

            event = AlertEvent(
                rule_id=rule.id,
                org_id=rule.org_id,
                agent_id=rule.agent_id,
                value=value,
                message=f"{rule.name}: {rule.metric} {rule.operator} {rule.threshold} (observed {value})",
                context={"metric": rule.metric, "window_minutes": rule.window_minutes},
            )
            db.add(event)
            summary["fired"] += 1
            try:
                asyncio.run(dispatch_alert(rule.channels or [], event.message, rule.severity))
            except RuntimeError:
                # if loop is already running just skip notifications
                pass
        db.commit()
    return summary
