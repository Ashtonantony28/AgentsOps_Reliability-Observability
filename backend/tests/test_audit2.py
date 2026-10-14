"""AUDIT-2 (performance under load) repros. Each is xfail(strict) until its PLAN.md sub-goal fixes it."""
import asyncio
import string
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import select

import app.services.trace_policy_service as tps
from app.models import AlertEvent, AlertRule, Incident, Trace, TracePolicy, TracePolicyViolation
from app.policy import ast
from app.policy.checker import Monitor, compile_policy
from app.policy.counterexample import minimal_counterexample, name_patterns
from app.policy.dsl import parse
from app.policy.events import Event
from app.services import alert_service, violation_service

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from bench_policy import synthetic_policies  # noqa: E402

RETRY_CAP = ast.Count(ast.And(ast.Match(kind="tool"), ast.Match(status="error")), "<=", 3, by="name")
NO_DROP = 'policy no_drop severity critical:\n  always not tool("drop_db")'


def retry_trace(n: int) -> list[Event]:
    """n events; the 4th error of the same tool is the last event, so the counterexample spans the whole trace."""
    bad = {n - 1 - j * (n // 4) for j in range(4)}
    return [Event(i, "tool", "fetch" if i in bad else f"t{i % 7}", "error" if i in bad else "success", {})
            for i in range(n)]


def _ms(f) -> float:
    t = time.perf_counter()
    f()
    return (time.perf_counter() - t) * 1000


@pytest.mark.xfail(strict=True, reason="AUDIT-2 A2-1: count-by counterexample is cubic in the window (SG-A2.1)")
def test_count_by_name_counterexample_meets_budget():
    p = compile_policy("retry", ast.Policy("retry_cap", "medium", RETRY_CAP))
    events = retry_trace(200)
    cx = []
    ms = _ms(lambda: cx.extend(minimal_counterexample(p, events, 199)))
    assert cx == [49, 99, 149, 199]
    assert ms <= 500, f"{ms:.0f} ms for 200 events (counterexample budget p95 500 ms)"


@pytest.mark.xfail(strict=True, reason="AUDIT-2 A2-7: glob-match sets are enumerated exhaustively (SG-A2.1)")
def test_overlapping_globs_counterexample_meets_budget():
    globs = tuple(f"*{c}*" for c in string.ascii_lowercase[:13])
    name_patterns.cache_clear()
    p = compile_policy("g", ast.Policy("g", "high", ast.Not(ast.Match(kind="tool", globs=globs))))
    ms = _ms(lambda: minimal_counterexample(p, [Event(0, "tool", "abc", "success", {})], 0))
    assert ms <= 500, f"{ms:.0f} ms for a 1-event trace and 13 globs"


@pytest.mark.xfail(strict=True, reason="AUDIT-2 A2-6: count-by-name state grows with distinct names (SG-A2.4)")
def test_ingest_step_with_many_distinct_names_meets_budget():
    compiled = [compile_policy(p.name, p) for p in synthetic_policies()]
    m = Monitor(compiled)
    n = 50_000
    for i in range(n):
        m.step(Event(i, "tool", f"tool_{i}", "error", {}))
    blob = m.snapshot()
    samples = sorted(_ms(lambda k=k: Monitor.restore(compiled, blob).step(Event(n, "tool", f"x{k}", "success", {})))
                     for k in range(20))
    assert len(blob) <= 64 * 1024, f"snapshot {len(blob) // 1024} KB"
    assert samples[18] <= 8, f"restore + step p95 {samples[18]:.1f} ms (ingest budget 8 ms)"


async def _stream(client, headers, agent, uid, n):
    for i in range(0, n, 500):
        r = await client.post(f"/api/traces/{uid}/events", headers=headers, json={
            "agent_id": agent.id, "events": [{"seq": s, "kind": "tool", "name": f"t{s % 5}"}
                                             for s in range(i, min(n, i + 500))]})
        assert r.status_code == 202, r.text


@pytest.mark.xfail(strict=True, reason="AUDIT-2 A2-3: precheck replays every span on each call after state loss "
                                       "(SG-A2.3)")
async def test_precheck_after_state_loss_replays_spans_once(client, db, org, agent, api_key_headers, redis,
                                                             monkeypatch):
    (p,) = parse(NO_DROP)
    db.add(TracePolicy(org_id=org.id, name=p.name, source=NO_DROP, severity=p.severity, action="block"))
    await db.commit()
    await _stream(client, api_key_headers, agent, "long", 1000)
    await redis.flushdb()  # Redis restart, eviction or the 24 h TTL
    replayed = []
    real = tps.events_from_spans
    monkeypatch.setattr(tps, "events_from_spans", lambda spans: replayed.append(len(spans)) or real(spans))
    for _ in range(3):
        r = await client.post("/api/trace-policies/check", headers=api_key_headers, json={
            "agent_id": agent.id, "trace_uid": "long", "candidate": {"seq": 0, "kind": "tool", "name": "drop_db"}})
        assert r.status_code == 200 and r.json()["allowed"] is False
    assert replayed == [1000], f"spans replayed per check: {replayed}"


@pytest.mark.xfail(strict=True, reason="AUDIT-2 A2-4: concurrent ingests both pass the alert dedup check (SG-A2.4)")
async def test_concurrent_violations_fire_a_rule_once(db, session_maker, org, agent, monkeypatch):
    db.add(AlertRule(org_id=org.id, name="any violation", metric="policy_violations", operator=">=", threshold=1,
                     window_minutes=15, dedup_minutes=30, severity="critical", context={}))
    (p,) = parse(NO_DROP)
    policy = TracePolicy(org_id=org.id, name=p.name, source=NO_DROP, severity=p.severity, action="log")
    db.add(policy)
    await db.commit()
    rows = []
    for uid in ("a", "b"):
        t = Trace(org_id=org.id, agent_id=agent.id, trace_uid=uid)
        db.add(t)
        await db.flush()
        rows.append(TracePolicyViolation(org_id=org.id, trace_id=t.id, agent_id=agent.id, policy_id=policy.id,
                                         policy_name=p.name, policy_version=1, severity="critical", at_seq=0,
                                         counterexample=[0], message="m", mode="live"))
        db.add(rows[-1])
    await db.commit()

    # two ingest workers: each has stored its violation and checks the rule before the other fires it
    arrived, real = [], alert_service.already_firing
    both = asyncio.Event()

    async def already_firing(s, rule):
        out = await real(s, rule)
        arrived.append(1)
        if len(arrived) == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 5)
        return out

    monkeypatch.setattr(violation_service, "already_firing", already_firing)

    async def ingest(row):
        async with session_maker() as s:
            await violation_service.evaluate_violation_alerts(s, [row])
            await s.commit()

    await asyncio.gather(*(ingest(r) for r in rows))
    async with session_maker() as s:
        alerts = (await s.execute(select(AlertEvent))).scalars().all()
        incidents = (await s.execute(select(Incident))).scalars().all()
    assert (len(alerts), len(incidents)) == (1, 1)
