import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from itertools import zip_longest
from typing import Optional, get_args

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AlertEvent, AlertRule, Incident, Trace, TracePolicy, TracePolicyViolation
from app.schemas.alert import MAX_WINDOW_MINUTES
from app.schemas.policy import PolicySeverity
from app.services.notification_service import dispatch_alert

log = logging.getLogger(__name__)

SEVERITIES = get_args(PolicySeverity)  # ascending
ALERTING_ACTIONS = ("alert", "block")  # `log` policies record violations without paging anyone
DISPATCH_CONCURRENCY = 20  # rules paging at once in one tick
DISPATCH_DEADLINE_S = 45.0  # the tick runs every minute
CHECK_DEADLINE_S = 180.0  # checks stop here, so the pages fired so far go out inside the task's 300 s limit


def severities_at_least(rule: AlertRule) -> tuple[str, ...]:
    floor = (rule.context or {}).get("severity_at_least")
    return SEVERITIES[SEVERITIES.index(floor):] if floor in SEVERITIES else SEVERITIES


def policy_violations_query(rule: AlertRule, since: datetime):
    q = select(func.count()).select_from(TracePolicyViolation).join(
        TracePolicy, TracePolicy.id == TracePolicyViolation.policy_id).where(
        TracePolicyViolation.org_id == rule.org_id, TracePolicyViolation.detected_at >= since,
        TracePolicyViolation.severity.in_(severities_at_least(rule)), TracePolicy.action.in_(ALERTING_ACTIONS))
    return q.where(TracePolicyViolation.agent_id == rule.agent_id) if rule.agent_id else q


def _traces(rule: AlertRule, since: datetime, *columns):
    q = select(*columns).where(Trace.org_id == rule.org_id, Trace.created_at >= since)
    return q.where(Trace.agent_id == rule.agent_id) if rule.agent_id else q


async def compute_metric_value(db: AsyncSession, rule: AlertRule) -> float:
    in_bound = 1 <= (rule.window_minutes or 0) <= MAX_WINDOW_MINUTES  # rows stored before the API checked may not be
    window = datetime.utcnow() - timedelta(minutes=rule.window_minutes if in_bound else 15)
    if rule.metric == "policy_violations":
        return float((await db.execute(policy_violations_query(rule, window))).scalar_one())
    if rule.metric == "cost_per_day":  # the last 24 h, whatever the rule's window
        q = select(func.coalesce(func.sum(Trace.cost_usd), 0.0)).where(
            Trace.org_id == rule.org_id, Trace.created_at >= datetime.utcnow() - timedelta(days=1))
        if rule.agent_id:
            q = q.where(Trace.agent_id == rule.agent_id)
        return float((await db.execute(q)).scalar_one())
    if rule.metric == "error_rate":
        total, errors = (await db.execute(_traces(
            rule, window, func.count(), func.count().filter(Trace.status != "success")))).one()
        return round(errors / total, 4) if total else 0.0
    if rule.metric == "latency_p95":  # numpy's linear percentile, from the two values around it
        total = (await db.execute(_traces(rule, window, func.count()))).scalar_one()
        if not total:
            return 0.0
        pos = 0.95 * (total - 1)
        near = (await db.execute(_traces(rule, window, Trace.latency_ms).order_by(Trace.latency_ms)
                                 .offset(int(pos)).limit(2))).scalars().all()
        return float(near[0] + (near[-1] - near[0]) * (pos - int(pos)))
    if rule.metric == "latency_avg":
        return float((await db.execute(_traces(rule, window, func.avg(Trace.latency_ms)))).scalar_one() or 0.0)
    if rule.metric == "token_usage":
        return float((await db.execute(_traces(
            rule, window, func.sum(Trace.tokens_input + Trace.tokens_output)))).scalar_one() or 0)
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
    minutes = rule.dedup_minutes if (rule.dedup_minutes or 0) >= 1 else 30  # rows stored before the API checked
    dedup = datetime.utcnow() - timedelta(minutes=minutes)
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
            severity="critical",
            timeline=[{
                "ts": datetime.utcnow().isoformat(),
                "event": "alert_fired",
                "detail": event.message,
            }],
        )
        db.add(inc)
    await db.commit()
    return event


async def _dispatch_all(pages: list[tuple]) -> None:
    """Sends each (org_id, channels, message, severity) page, a bounded number at once taking turns by org, and
    cancels what is still sending at the deadline."""
    by_org = defaultdict(list)
    for org_id, *page in pages:
        by_org[org_id].append(page)
    gate = asyncio.Semaphore(DISPATCH_CONCURRENCY)

    async def send(page):
        async with gate:
            try:
                await dispatch_alert(*page)
            except Exception:
                log.exception("alert dispatch failed")

    tasks = [asyncio.create_task(send(p)) for turn in zip_longest(*by_org.values()) for p in turn if p is not None]
    if not tasks:
        return
    _, pending = await asyncio.wait(tasks, timeout=DISPATCH_DEADLINE_S)
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
        log.warning("%d alert dispatch(es) still sending at the %ss deadline were cancelled", len(pending),
                    DISPATCH_DEADLINE_S)


async def _check_all(db: AsyncSession, summary: dict, pages: list) -> None:
    rules = (await db.execute(select(AlertRule).where(
        AlertRule.enabled.is_(True), AlertRule.metric != "policy_violations").order_by(AlertRule.created_at))).scalars().all()
    db.expunge_all()  # a rollback after a failed check would expire the rules still to check
    for rule in rules:
        summary["checked"] += 1
        try:
            value = await compute_metric_value(db, rule)
            if not compare(value, rule.operator, rule.threshold) or await already_firing(db, rule):
                continue
            event = await fire_alert(db, rule, value)
        except Exception:
            log.exception("alert rule %s check failed, skipped", rule.id)
            await db.rollback()
            continue
        summary["fired"] += 1
        if rule.channels:
            pages.append((rule.org_id, rule.channels, f"{rule.name}: {event.message}", rule.severity))


async def evaluate_metric_rules(db: AsyncSession) -> dict:
    """The periodic check of every enabled metric rule (policy_violations rules fire as violations are stored).
    Every rule is checked before any is paged, so no org's channels hold up another org's rules; a rule whose check
    raises is skipped, and the pages fired so far are sent however the check phase ends."""
    summary = {"checked": 0, "fired": 0}
    pages = []
    try:
        await asyncio.wait_for(_check_all(db, summary, pages), CHECK_DEADLINE_S)
    except asyncio.TimeoutError:
        log.warning("alert checks stopped at the %ss deadline after %d rule(s)", CHECK_DEADLINE_S, summary["checked"])
    finally:
        await _dispatch_all(pages)
    return summary


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
