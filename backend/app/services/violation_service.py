import asyncio
import logging
import weakref
from typing import Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AlertRule, TracePolicy, TracePolicyViolation
from app.redis import get_redis
from app.schemas.policy import ViolationOut
from app.services import trace_policy_service as tps
from app.services.alert_service import ALERTING_ACTIONS, already_firing, compare, compute_metric_value, fire_alert, severities_at_least
from app.services.notification_service import dispatch_alert

log = logging.getLogger(__name__)
_notifications: set[asyncio.Task] = set()
_rule_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


async def publish_violations(db: AsyncSession, rows: Sequence[TracePolicyViolation]) -> None:
    """Fans newly stored violations out to `violations:{org_id}` and `policy_violations` alert rules.
    Runs after the rows are committed and never raises: ingestion must not fail on a feed or alert error."""
    if not rows:
        return
    try:
        pipe = get_redis().pipeline(transaction=False)
        for row in rows:
            pipe.publish(f"violations:{row.org_id}", ViolationOut.model_validate(row).model_dump_json())
        await pipe.execute()
    except Exception:
        log.exception("could not publish %d policy violations", len(rows))
    try:
        async with AsyncSession(db.bind, expire_on_commit=False) as s:  # keeps the caller's objects unexpired
            await evaluate_violation_alerts(s, rows)
    except Exception:
        log.exception("could not evaluate policy_violations alert rules")


async def evaluate_violation_alerts(db: AsyncSession, rows: Sequence[TracePolicyViolation]) -> None:
    alerting = set((await db.execute(select(TracePolicy.id).where(
        TracePolicy.id.in_({r.policy_id for r in rows}), TracePolicy.action.in_(ALERTING_ACTIONS)))).scalars())
    rows = [r for r in rows if r.policy_id in alerting]
    if not rows:
        return
    rules = (await db.execute(select(AlertRule).where(
        AlertRule.org_id == rows[0].org_id, AlertRule.enabled == True,  # noqa: E712
        AlertRule.metric == "policy_violations",
        or_(AlertRule.agent_id.is_(None), AlertRule.agent_id.in_({r.agent_id for r in rows})),
    ).order_by(AlertRule.created_at))).scalars().all()
    for rule in rules:
        allowed = severities_at_least(rule)
        hits = [r for r in rows if rule.agent_id in (None, r.agent_id) and r.severity in allowed]
        if not hits:
            continue
        value = await compute_metric_value(db, rule)
        if not compare(value, rule.operator, rule.threshold) or await already_firing(db, rule):
            continue
        # concurrent ingests serialise here, in process and on the rule's row, then re-check; fire_alert commits
        lock = _rule_locks.setdefault(rule.id, asyncio.Lock())
        async with lock:
            await db.execute(select(AlertRule.id).where(AlertRule.id == rule.id).with_for_update())
            if await already_firing(db, rule):
                await db.commit()  # releases the row lock before the next rule's
                continue
            event = await fire_alert(db, rule, value, context={"violation_ids": [h.id for h in hits]},
                                     incident=any(h.severity == "critical" for h in hits))
        if rule.channels:
            task = asyncio.create_task(dispatch_alert(rule.channels, f"{rule.name}: {event.message}", rule.severity))
            _notifications.add(task)
            task.add_done_callback(_notifications.discard)


async def drain_notifications() -> None:
    await asyncio.gather(*_notifications, return_exceptions=True)


async def check_and_publish(db: AsyncSession, trace_id: str,
                            counterexamples: bool = True) -> list[TracePolicyViolation]:
    rows = await tps.check_completed_trace(db, trace_id, counterexamples)
    await publish_violations(db, rows)
    return rows
