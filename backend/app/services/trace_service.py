import json
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any, Optional

from redis.exceptions import LockError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agent, Trace, Span, PolicyRule, AuditLog, TracePolicyViolation
from app.policy.checker import Monitor
from app.policy.events import Event, event_inputs
from app.redis import get_redis
from app.schemas.policy import EventsAppendIn, PolicyCheckIn, PolicyCheckOut, ViolationOut
from app.schemas.trace import MAX_TRACE_EVENTS, EventIn, TraceIn
from app.services import trace_policy_service as tps
from app.services.security_service import detect_pii, injection_score, mask_pii
from app.services.violation_service import publish_violations
from app.tasks.policy_tasks import enqueue_trace_check


class PolicyViolation(Exception):
    def __init__(self, message: str, rule_name: str, rule_id: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.rule_name = rule_name
        self.rule_id = rule_id


class TraceConflict(Exception):
    """Appending to this trace would reorder, duplicate or reopen events (HTTP 409)."""
    status = 409

    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


class TraceTooLong(TraceConflict):
    status = 413


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
        .order_by(PolicyRule.created_at, PolicyRule.id)
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
        if rule.get("forbid_pii") and any(detect_pii(t) for t in _texts(trace, events, "input", "output")):
            hits.append(("PII detected (blocked by policy)", "warn: pii detected"))
        if rule.get("block_on_injection") and max(injection_score(t)[0] for t in _texts(trace, events, "input")) \
                >= rule.get("injection_threshold", 0.34):
            hits.append(("prompt injection detected", "warn: injection detected"))
        if hits and r.action == "block":
            raise PolicyViolation(hits[0][0], r.name, r.id)
        if hits and note is None:
            note = hits[0][1]
    return note


def _event_trace(agent: Agent, environment: str, events: list[EventIn]) -> TraceIn:
    """The legacy-rule view of events checked on their own (event append and precheck): no trace text."""
    return TraceIn.model_construct(agent_id=agent.id, environment=environment, input="", output="",
                                   events=events, tool_calls=[])


def _strings(v: Any):
    if isinstance(v, str):
        yield v
    elif isinstance(v, (dict, list)):
        for x in v.values() if isinstance(v, dict) else v:
            yield from _strings(x)


def _texts(trace: TraceIn, events: list[EventIn], *sides: str) -> list[str]:
    """Trace text plus each string in the event payloads, scanned separately so matches never span fields."""
    return [t for side in sides
            for t in [getattr(trace, side), *(s for e in events for s in _strings(getattr(e, side)))]]


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


def _trace_policy_block(name: str, seq: int) -> PolicyViolation:
    return PolicyViolation(f"trace policy '{name}' violated at seq {seq}", name)


async def ingest_trace(db: AsyncSession, org_id: str, payload: TraceIn) -> Trace:
    agent = (await db.execute(
        select(Agent).where(Agent.id == payload.agent_id, Agent.org_id == org_id)
    )).scalar_one_or_none()
    if not agent:
        raise ValueError("agent_not_found")
    if payload.trace_id:
        existing = (await db.execute(
            select(Trace).where(Trace.org_id == org_id, Trace.trace_uid == payload.trace_id).limit(1)
        )).scalar_one_or_none()
        if existing is not None:
            return existing
    events = event_inputs(payload)

    # policy enforcement
    try:
        policy_note = await evaluate_policies(db, org_id, agent, payload, events)
        policies = await tps.active_policies(db, org_id, agent.id, payload.environment)
        blocked = tps.blocking_violations(policies, _events(events))
        if blocked:
            raise _trace_policy_block(blocked[0].policy_name, blocked[0].at_seq)
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
        created_at=_naive_utc(payload.timestamp) or datetime.utcnow(),
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
        if next_seq + len(req.events) > MAX_TRACE_EVENTS:
            raise TraceTooLong("trace_too_long", f"a trace holds at most {MAX_TRACE_EVENTS} events",
                               max_events=MAX_TRACE_EVENTS)
        if trace is None:
            trace = Trace(org_id=org_id, agent_id=agent.id, trace_uid=trace_uid, environment=agent.environment,
                          agent_version=agent.current_version, meta={"stream": "open"})
            db.add(trace)
            await db.flush()

        try:
            legacy = _event_trace(agent, trace.environment, req.events)
            policy_note = await evaluate_policies(db, org_id, agent, legacy, req.events)
            policies = await tps.active_policies(db, org_id, agent.id, trace.environment)
            monitor, replayed = await tps.load_monitor(db, trace.id, [c for _, c in policies], next_seq)
            blocking = {row.id: c.policy.name for row, c in policies if row.action == "block"}
            found = []
            for e in _events(req.events):
                found += monitor.step(e)
                blocked = [blocking[pid] for pid in monitor.failing if pid in blocking]
                if blocked:
                    raise _trace_policy_block(blocked[0], e.seq)
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
    await publish_violations(db, rows)
    if req.final:
        await enqueue_trace_check(trace.id)
    return trace, rows


async def precheck(db: AsyncSession, org_id: str, req: PolicyCheckIn) -> PolicyCheckOut:
    """Evaluates `req.candidate` as the next event of the stored trace (its own seq is ignored); stores nothing."""
    agent = (await db.execute(
        select(Agent).where(Agent.id == req.agent_id, Agent.org_id == org_id)
    )).scalar_one_or_none()
    if not agent:
        raise ValueError("agent_not_found")
    trace = None if req.trace_uid is None else (await db.execute(
        select(Trace).where(Trace.org_id == org_id, Trace.trace_uid == req.trace_uid)
        .order_by(Trace.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    if trace is not None and trace.agent_id != agent.id:
        raise TraceConflict("agent_mismatch", "trace belongs to another agent")
    environment = agent.environment if trace is None else trace.environment
    policies = await tps.active_policies(db, org_id, agent.id, environment)
    compiled = [c for _, c in policies]
    if trace is None:
        monitor = Monitor(compiled)
    else:
        last = (await db.execute(select(func.max(Span.seq)).where(Span.trace_id == trace.id))).scalar_one()
        monitor = await tps.precheck_monitor(db, trace.id, compiled, 0 if last is None else last + 1)
    c = req.candidate
    found = monitor.check(Event(monitor.next_seq, c.kind, c.name, c.status, dict(c.attrs)))
    rows = {row.id: row for row, _ in policies}
    now = datetime.utcnow()
    legacy = []
    try:
        await evaluate_policies(db, org_id, agent, _event_trace(agent, environment, [c]), [c])
    except PolicyViolation as pv:  # a legacy block rule: the append would be a 403 too
        legacy = [ViolationOut(
            agent_id=agent.id, policy_id=pv.rule_id, policy_name=pv.rule_name, policy_version=1, severity="high",
            at_seq=monitor.next_seq, counterexample=[], message=pv.message, mode="precheck", detected_at=now,
        )]
    return PolicyCheckOut(
        allowed=not legacy and not any(rows[v.policy_id].action == "block" for v in found),
        violations=legacy + [ViolationOut(
            agent_id=agent.id, policy_id=v.policy_id, policy_name=v.policy_name,
            policy_version=rows[v.policy_id].version, severity=v.severity, at_seq=v.at_seq, counterexample=[],
            message=v.message, mode="precheck", detected_at=now,
        ) for v in found],
    )
