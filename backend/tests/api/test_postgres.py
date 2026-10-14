"""Code paths that exist only for Postgres (AUDIT-4 A4-4): the on_conflict insert and the alert rule's row lock."""
import asyncio

import pytest
from sqlalchemy import select

import app.services.trace_policy_service as tps
from app.models import AlertEvent, AlertRule, Trace, TracePolicyViolation
from app.services import alert_service, violation_service
from tests.api.test_event_ingest import CONFIRM, NO_DROP, _append, _policy, _rows, ev

pytestmark = pytest.mark.postgres


async def test_runs_on_postgres(engine):
    assert engine.dialect.name == "postgresql"


async def test_violation_stored_once_per_trace_and_version(client, db, session_maker, org, agent, api_key_headers):
    await _policy(db, org, CONFIRM)
    await _append(client, api_key_headers, agent, "u1", [ev(0, "search"), ev(1, "delete_user")], final=True)
    (trace,) = await _rows(db, Trace)
    assert await tps.check_completed_trace(db, trace.id) == []  # the live row is already stored
    async with session_maker() as fresh:
        rows = await _rows(fresh, TracePolicyViolation)
    assert [(r.mode, r.at_seq, r.counterexample) for r in rows] == [("live", 1, [0, 1])]  # counterexample filled in


async def test_rule_row_lock_serialises_alerts_across_workers(db, session_maker, org, agent, monkeypatch):
    """Two workers in separate processes: the in-process lock is not shared, so only the row lock keeps the
    second from firing before the first commits."""
    db.add(AlertRule(org_id=org.id, name="any violation", metric="policy_violations", operator=">=", threshold=1,
                     window_minutes=15, dedup_minutes=30, severity="high", context={}))
    policy = await _policy(db, org, NO_DROP, action="alert")
    rows = []
    for uid in ("a", "b"):
        t = Trace(org_id=org.id, agent_id=agent.id, trace_uid=uid)
        db.add(t)
        await db.flush()
        rows.append(TracePolicyViolation(org_id=org.id, trace_id=t.id, agent_id=agent.id, policy_id=policy.id,
                                         policy_name=policy.name, policy_version=1, severity="critical",
                                         at_seq=0, counterexample=[0], message="m", mode="live"))
        db.add(rows[-1])
    await db.commit()

    class PerWorker(dict):
        def setdefault(self, key, default):
            return default

    rechecked, real_fire = asyncio.Event(), alert_service.fire_alert
    calls = []
    real_firing = alert_service.already_firing

    async def already_firing(s, rule):
        calls.append(1)
        if len(calls) == 4:  # both workers re-checked under the lock: without the row lock neither saw the other
            rechecked.set()
        return await real_firing(s, rule)

    async def fire_alert(s, rule, *a, **kw):
        await asyncio.sleep(0)
        try:
            await asyncio.wait_for(rechecked.wait(), 0.5)
        except asyncio.TimeoutError:
            pass  # the other worker is blocked on the row lock until this one commits
        return await real_fire(s, rule, *a, **kw)

    monkeypatch.setattr(violation_service, "_rule_locks", PerWorker())
    monkeypatch.setattr(violation_service, "already_firing", already_firing)
    monkeypatch.setattr(violation_service, "fire_alert", fire_alert)

    async def worker(row):
        async with session_maker() as s:
            await violation_service.evaluate_violation_alerts(s, [row])
            await s.commit()

    await asyncio.gather(*(worker(r) for r in rows))
    async with session_maker() as s:
        assert len((await s.execute(select(AlertEvent))).scalars().all()) == 1
