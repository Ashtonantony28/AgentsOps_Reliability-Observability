import json
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any, Optional

from redis.exceptions import LockError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agent, Trace, Span, PolicyRule, AuditLog, TracePolicyViolation
from app.policy.events import Event, event_inputs
from app.redis import get_redis
from app.schemas.policy import EventsAppendIn
from app.schemas.trace import EventIn, TraceIn
from app.services import trace_policy_service as tps
from app.services.security_service import detect_pii, injection_score, mask_pii
from app.tasks.policy_tasks import enqueue_trace_check


class PolicyViolation(Exception):
    def __init__(self, message: str, rule_name: str):
        super().__init__(message)
        self.message = message
        self.rule_name = rule_name


class TraceConflict(Exception):
    """Appending to this trace would reorder, duplicate or reopen events (HTTP 409)."""

    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def _naive_utc(ts: Optional[datetime]) -> Optional[datetime]:
    if ts is None or ts.tzinfo is None:
        return ts
    return ts.astimezone(timezone.utc).replace(tzinfo=None)


async def evaluate_policies(db: AsyncSession, org_id: str, agent: Agent, trace: TraceIn,
                            events: Optional[list[EventIn]] = None) -> Optional[str]:
    """Raises on the first matching `block` rule; otherwise returns the first warning, if any."""
    events = event_inputs(trace) if events is None else events
    rules = (await db.execute(
        select(PolicyRule).where(PolicyRule.org_id == org_id, PolicyRule.enabled == True)  # noqa
    )).scalars().all()
    note = None
    for r in rules:
        rule = r.rule or {}
        if rule.get("agent_id") and rule["agent_id"] != agent.id:
            continue
        if rule.get("environment") and rule["environment"] != trace.environment:
            continue
        hits = []
        tool = rule.get("forbid_tool")
        if tool and any(ev.name == tool for ev in events):  # every kind: `kind` is client-chosen
            hits.append((f"tool '{tool}' forbidden by policy", f"warn: tool {tool} flagged"))
        if rule.get("forbid_pii") and (detect_pii(trace.input) or detect_pii(trace.output)):
            hits.append(("PII detected (blocked by policy)", "warn: pii detected"))
        if rule.get("block_on_injection") and \
                injection_score(trace.input)[0] >= rule.get("injection_threshold", 0.34):
            hits.append(("prompt injection detected", "warn: injection detected"))
        if hits and r.action == "block":
            raise PolicyViolation(hits[0][0], r.name)
        if hits and note is None:
            note = hits[0][1]
    return note


def _text(payloads: list[Any]) -> str:
    """String leaves of event payloads, so text rules can scan streamed traces."""
    out, stack = [], list(payloads)
    while stack:
        v = stack.pop()
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)
    return "\n".join(out)


def _events(events: list[EventIn]) -> list[Event]:
    return [Event(e.seq, e.kind, e.name, e.status, dict(e.attrs)) for e in events]


def _span(trace_id: str, ev: EventIn) -> Span:
    return Span(trace_id=trace_id, seq=ev.seq, kind=ev.kind, name=ev.name, input=ev.input, output=ev.output,
                attrs=ev.attrs or None, latency_ms=ev.latency_ms, status=ev.status,
                started_at=_naive_utc(ev.timestamp) or datetime.utcnow(), ended_at=datetime.utcnow())


async def _block(db: AsyncSession, org_id: str, agent_id: str, pv: PolicyViolation, **details) -> None:
    await db.rollback()
    db.add(AuditLog(
        org_id=org_id, actor_type="agent", actor_id=agent_id,
        action="policy_block", resource_type="trace",
        rationale=pv.message, details={"rule": pv.rule_name, **details},
    ))
    await db.commit()


def _trace_policy_block(v) -> PolicyViolation:
    return PolicyViolation(f"trace policy '{v.policy_name}' violated at seq {v.at_seq}", v.policy_name)


async def ingest_trace(db: AsyncSession, org_id: str, payload: TraceIn) -> Trace:
    agent = (await db.execute(
        select(Agent).where(Agent.id == payload.agent_id, Agent.org_id == org_id)
    )).scalar_one_or_none()
    if not agent:
        raise ValueError("agent_not_found")
    events = event_inputs(payload)

    # policy enforcement
    try:
        policy_note = await evaluate_policies(db, org_id, agent, payload, events)
        policies = await tps.active_policies(db, org_id, agent.id, payload.environment)
        blocked = tps.blocking_violations(policies, _events(events))
        if blocked:
            raise _trace_policy_block(blocked[0])
    except PolicyViolation as pv:
        await _block(db, org_id, agent.id, pv)
        raise

    pii = detect_pii(payload.input) + detect_pii(payload.output)
    score, _hits = injection_score(payload.input)

    input_text = mask_pii(payload.input) if pii else payload.input
    output_text = mask_pii(payload.output) if pii else payload.output

    trace = Trace(
        org_id=org_id,
        agent_id=agent.id,
        session_id=payload.session_id,
        trace_uid=payload.trace_id or str(uuid.uuid4()),
        status=payload.status,
        input=input_text,
        output=output_text,
        error_message=payload.error_message,
        latency_ms=payload.latency_ms,
        tokens_input=payload.tokens_input,
        tokens_output=payload.tokens_output,
        cost_usd=payload.cost_usd,
        environment=payload.environment,
        agent_version=payload.agent_version,
        pii_flags=list(set(pii)),
        injection_score=score,
        meta=payload.metadata or {},
        created_at=payload.timestamp or datetime.utcnow(),
    )
    db.add(trace)
    await db.flush()

    for ev in events:
        db.add(_span(trace.id, ev))

    await db.commit()
    if policies:
        await enqueue_trace_check(trace.id)

    # Publish to Redis pub/sub for real-time feed
    r = get_redis()
    await r.publish(
        f"traces:{org_id}",
        json.dumps({
            "id": trace.id,
            "agent_id": trace.agent_id,
            "status": trace.status,
            "latency_ms": trace.latency_ms,
            "cost_usd": trace.cost_usd,
            "created_at": trace.created_at.isoformat(),
            "input_preview": (trace.input or "")[:200],
        })
    )

    if policy_note:
        db.add(AuditLog(
            org_id=org_id, actor_type="agent", actor_id=agent.id,
            action="policy_warn", resource_type="trace", resource_id=trace.id,
            rationale=policy_note,
        ))
        await db.commit()

    return trace


async def append_events(db: AsyncSession, org_id: str, trace_uid: str,
                        req: EventsAppendIn) -> tuple[Trace, list[TracePolicyViolation]]:
    """Appends events to a streamed trace and checks them live; `final` closes the trace and queues the batch check."""
    lock = get_redis().lock(f"lock:trace-events:{org_id}:{trace_uid}", timeout=60, blocking_timeout=10)
    if not await lock.acquire():
        raise TraceConflict("trace_busy", "another append to this trace is in progress; retry")
    try:
        agent = (await db.execute(
            select(Agent).where(Agent.id == req.agent_id, Agent.org_id == org_id)
        )).scalar_one_or_none()
        if not agent:
            raise ValueError("agent_not_found")
        trace = (await db.execute(
            select(Trace).where(Trace.org_id == org_id, Trace.trace_uid == trace_uid)
            .order_by(Trace.created_at.desc()).limit(1)
        )).scalar_one_or_none()
        if trace is not None and (trace.meta or {}).get("stream") != "open":
            raise TraceConflict("trace_closed", "trace is complete; no more events can be appended")
        if trace is not None and trace.agent_id != agent.id:
            raise TraceConflict("agent_mismatch", "trace belongs to another agent")
        last = None if trace is None else (await db.execute(
            select(func.max(Span.seq)).where(Span.trace_id == trace.id))).scalar_one()
        next_seq = 0 if last is None else last + 1
        if [e.seq for e in req.events] != list(range(next_seq, next_seq + len(req.events))):
            raise TraceConflict("seq_conflict", f"events must continue at seq {next_seq} without gaps",
                                expected_seq=next_seq)
        if trace is None:
            trace = Trace(org_id=org_id, agent_id=agent.id, trace_uid=trace_uid, environment=agent.environment,
                          agent_version=agent.current_version, meta={"stream": "open"})
            db.add(trace)
            await db.flush()

        try:
            legacy = TraceIn.model_construct(agent_id=agent.id, environment=trace.environment,
                                             input=_text([e.input for e in req.events]),
                                             output=_text([e.output for e in req.events]),
                                             events=req.events, tool_calls=[])
            policy_note = await evaluate_policies(db, org_id, agent, legacy, req.events)
            policies = await tps.active_policies(db, org_id, agent.id, trace.environment)
            monitor, replayed = await tps.load_monitor(db, trace.id, [c for _, c in policies], next_seq)
            found = [v for e in _events(req.events) for v in monitor.step(e)]
            actions = {row.id: row.action for row, _ in policies}
            blocked = [v for v in found if actions[v.policy_id] == "block"]
            if blocked:
                raise _trace_policy_block(blocked[0])
        except PolicyViolation as pv:
            await _block(db, org_id, agent.id, pv, trace_uid=trace_uid)
            raise

        for ev in req.events:
            db.add(_span(trace.id, ev))
        rows = await tps.record_violations(db, trace, policies, replayed + found, "live")
        if policy_note:
            db.add(AuditLog(
                org_id=org_id, actor_type="agent", actor_id=agent.id,
                action="policy_warn", resource_type="trace", resource_id=trace.id, rationale=policy_note,
            ))
        if req.final:
            trace.meta = {**(trace.meta or {}), "stream": "final"}
        await db.commit()
        await tps.save_monitor(trace.id, None if req.final else monitor)
    finally:
        with suppress(LockError):
            await lock.release()
    if req.final:
        await enqueue_trace_check(trace.id)
    return trace, rows
