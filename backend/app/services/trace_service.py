import json
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agent, Trace, Span, PolicyRule, AuditLog
from app.policy.events import event_inputs
from app.redis import get_redis
from app.schemas.trace import TraceIn
from app.services.security_service import detect_pii, injection_score, mask_pii


class PolicyViolation(Exception):
    def __init__(self, message: str, rule_name: str):
        super().__init__(message)
        self.message = message
        self.rule_name = rule_name


async def evaluate_policies(db: AsyncSession, org_id: str, agent: Agent, trace: TraceIn) -> Optional[str]:
    rules = (await db.execute(
        select(PolicyRule).where(PolicyRule.org_id == org_id, PolicyRule.enabled == True)  # noqa
    )).scalars().all()
    for r in rules:
        rule = r.rule or {}
        if rule.get("agent_id") and rule["agent_id"] != agent.id:
            continue
        if rule.get("environment") and rule["environment"] != trace.environment:
            continue
        if rule.get("forbid_tool"):
            tool = rule["forbid_tool"]
            for ev in event_inputs(trace):
                if ev.kind == "tool" and ev.name == tool:
                    if r.action == "block":
                        raise PolicyViolation(f"tool '{tool}' forbidden by policy", r.name)
                    return f"warn: tool {tool} flagged"
        if rule.get("forbid_pii"):
            if detect_pii(trace.input) or detect_pii(trace.output):
                if r.action == "block":
                    raise PolicyViolation("PII detected (blocked by policy)", r.name)
                return "warn: pii detected"
        if rule.get("block_on_injection"):
            score, _ = injection_score(trace.input)
            if score >= rule.get("injection_threshold", 0.34):
                if r.action == "block":
                    raise PolicyViolation("prompt injection detected", r.name)
                return "warn: injection detected"
    return None


async def ingest_trace(db: AsyncSession, org_id: str, payload: TraceIn) -> Trace:
    agent = (await db.execute(
        select(Agent).where(Agent.id == payload.agent_id, Agent.org_id == org_id)
    )).scalar_one_or_none()
    if not agent:
        raise ValueError("agent_not_found")

    # policy enforcement
    try:
        policy_note = await evaluate_policies(db, org_id, agent, payload)
    except PolicyViolation as pv:
        db.add(AuditLog(
            org_id=org_id, actor_type="agent", actor_id=agent.id,
            action="policy_block", resource_type="trace",
            rationale=pv.message, details={"rule": pv.rule_name},
        ))
        await db.commit()
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

    for ev in event_inputs(payload):
        db.add(Span(
            trace_id=trace.id,
            seq=ev.seq,
            kind=ev.kind,
            name=ev.name,
            input=ev.input,
            output=ev.output,
            attrs=ev.attrs or None,
            latency_ms=ev.latency_ms,
            status=ev.status,
            started_at=ev.timestamp or datetime.utcnow(),
            ended_at=datetime.utcnow(),
        ))

    await db.commit()

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
