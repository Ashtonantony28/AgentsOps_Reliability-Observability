import asyncio
import logging
import uuid
import weakref
from contextlib import suppress
from datetime import datetime
from functools import lru_cache
from typing import Sequence

from redis.exceptions import LockError
from sqlalchemy import or_, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Span, Trace, TracePolicy, TracePolicyViolation
from app.policy.checker import CompiledPolicy, Monitor, Violation, check_trace, check_verdicts, compile_policy
from app.policy.dsl import PolicyParseError, parse
from app.policy.events import Event, events_from_spans
from app.redis import get_redis

log = logging.getLogger(__name__)
STATE_TTL_S = 24 * 3600

Active = list[tuple[TracePolicy, CompiledPolicy]]


@lru_cache(maxsize=2048)
def _compile(policy_id: str, source: str) -> CompiledPolicy:
    (p,) = parse(source)
    return compile_policy(policy_id, p)


async def active_policies(db: AsyncSession, org_id: str, agent_id: str, environment: str) -> Active:
    rows = (await db.execute(
        select(TracePolicy).where(
            TracePolicy.org_id == org_id, TracePolicy.enabled == True,  # noqa: E712
            or_(TracePolicy.agent_id.is_(None), TracePolicy.agent_id == agent_id),
            or_(TracePolicy.environment.is_(None), TracePolicy.environment == environment),
        ).order_by(TracePolicy.id)
    )).scalars().all()
    out = []
    for r in rows:
        try:
            out.append((r, _compile(r.id, r.source)))
        except (PolicyParseError, ValueError):
            log.warning("skipping trace policy %s: source does not parse to one policy", r.id)
    return out


async def record_violations(db: AsyncSession, trace: Trace, policies: Active, violations: Sequence[Violation],
                            mode: str) -> list[TracePolicyViolation]:
    """Inserts violations not already stored for (trace, policy, version); returns the new rows."""
    if not violations:
        return []
    by_id = {row.id: row for row, _ in policies}
    now = datetime.utcnow()
    values = [dict(
        id=str(uuid.uuid4()), org_id=trace.org_id, trace_id=trace.id, agent_id=trace.agent_id,
        policy_id=v.policy_id, policy_name=v.policy_name, policy_version=by_id[v.policy_id].version,
        severity=v.severity, at_seq=v.at_seq, counterexample=list(v.counterexample), message=v.message,
        mode=mode, detected_at=now,
    ) for v in violations]
    insert = postgresql.insert if db.get_bind().dialect.name == "postgresql" else sqlite.insert
    stmt = (insert(TracePolicyViolation).values(values)
            .on_conflict_do_nothing(index_elements=["trace_id", "policy_id", "policy_version"])
            .returning(TracePolicyViolation.id))
    ids = set((await db.execute(stmt)).scalars().all())
    return [TracePolicyViolation(**v) for v in values if v["id"] in ids]


def _state_key(trace_id: str) -> str:
    return f"policy:monitor:{trace_id}"


def _precheck_key(trace_id: str) -> str:
    return f"policy:precheck:{trace_id}"


# the columns events_from_spans reads; full rows carry payloads the checker never needs
_EVENT_COLUMNS = (Span.id, Span.seq, Span.kind, Span.name, Span.status, Span.attrs, Span.started_at)
MAX_CONCURRENT_REBUILDS = 4
_rebuild_slots: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # per event loop


def _restore(blob: str | None, compiled: Sequence[CompiledPolicy], next_seq: int) -> Monitor | None:
    if blob:
        with suppress(ValueError):
            m = Monitor.restore(compiled, blob.encode())
            if m.next_seq == next_seq:
                return m
    return None


async def _replay(db: AsyncSession, trace_id: str,
                  compiled: Sequence[CompiledPolicy]) -> tuple[Monitor, list[Violation]]:
    """Rebuilds from spans. After a Redis restart every open trace needs this, so few run at once per process."""
    loop = asyncio.get_running_loop()
    slots = _rebuild_slots.get(loop) or _rebuild_slots.setdefault(loop, asyncio.Semaphore(MAX_CONCURRENT_REBUILDS))
    async with slots:
        spans = (await db.execute(select(*_EVENT_COLUMNS).where(Span.trace_id == trace_id))).all()
        m = Monitor(compiled)
        return m, [v for e in events_from_spans(spans) for v in m.step(e)]


async def load_monitor(db: AsyncSession, trace_id: str, compiled: Sequence[CompiledPolicy],
                       next_seq: int) -> tuple[Monitor, list[Violation]]:
    """Monitor positioned after the stored events. Rebuilt from spans if the Redis state is missing, stale or
    for other policies; the violations found while rebuilding are returned so they can be recorded."""
    m = _restore(await get_redis().get(_state_key(trace_id)), compiled, next_seq)
    if m:
        return m, []
    await get_redis().delete(_precheck_key(trace_id))  # the state this append saves supersedes it
    return await _replay(db, trace_id, compiled)


async def precheck_monitor(db: AsyncSession, trace_id: str, compiled: Sequence[CompiledPolicy],
                           next_seq: int) -> Monitor:
    """As `load_monitor`, but a rebuild is cached under its own key, one rebuild per trace at a time: it must not
    stand in for the append path's state, whose own rebuild records the violations it finds."""
    r = get_redis()
    keys = (_state_key(trace_id), _precheck_key(trace_id))
    for blob in await r.mget(keys):
        if m := _restore(blob, compiled, next_seq):
            return m
    lock = r.lock(f"lock:monitor-rebuild:{trace_id}", timeout=30, blocking_timeout=5)
    locked = await lock.acquire()
    try:
        if m := _restore(await r.get(keys[1]), compiled, next_seq):  # built while we waited
            return m
        m, _ = await _replay(db, trace_id, compiled)
        await r.set(keys[1], m.snapshot().decode(), ex=STATE_TTL_S)
        return m
    finally:
        if locked:
            with suppress(LockError):
                await lock.release()


async def save_monitor(trace_id: str, monitor: Monitor | None) -> None:
    if monitor is None:
        await get_redis().delete(_state_key(trace_id), _precheck_key(trace_id))
    else:
        await get_redis().set(_state_key(trace_id), monitor.snapshot().decode(), ex=STATE_TTL_S)


def blocking_violations(policies: Active, events: Sequence[Event]) -> list[Violation]:
    compiled = [c for row, c in policies if row.action == "block"]
    if not compiled:
        return []
    m = Monitor(compiled)
    return [v for e in events for v in m.step(e)]


async def check_completed_trace(db: AsyncSession, trace_id: str,
                                counterexamples: bool = True) -> list[TracePolicyViolation]:
    """Batch check of a completed trace; re-running it stores nothing new."""
    trace = await db.get(Trace, trace_id)
    if trace is None or (trace.meta or {}).get("stream") == "open":
        return []
    policies = await active_policies(db, trace.org_id, trace.agent_id, trace.environment)
    spans = (await db.execute(select(Span).where(Span.trace_id == trace_id))).scalars().all()
    check = check_trace if counterexamples else check_verdicts
    violations = check([c for _, c in policies], events_from_spans(spans))
    rows = await record_violations(db, trace, policies, violations, "batch")
    # live rows were stored before counterexamples existed; fill them in from the batch result
    with_cx = {v.policy_id: list(v.counterexample) for v in violations if v.counterexample}
    if with_cx:
        versions = {row.id: row.version for row, _ in policies}
        stored = (await db.execute(select(TracePolicyViolation).where(
            TracePolicyViolation.trace_id == trace_id, TracePolicyViolation.policy_id.in_(with_cx)))).scalars().all()
        for s in stored:
            if not s.counterexample and s.policy_version == versions[s.policy_id]:
                s.counterexample = with_cx[s.policy_id]
    await db.commit()
    return rows
