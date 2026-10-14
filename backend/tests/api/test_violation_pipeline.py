import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient

from app.celery_app import celery
from app.database import Base
from app.middleware.auth import make_access_token
from app.models import (
    Agent, AlertEvent, AlertRule, Incident, Organization, OrgMembership, Trace, TracePolicy, TracePolicyViolation, User,
)
from app.policy.dsl import parse, pretty
from app.schemas.policy import ViolationMessage, ViolationOut
from app.services import violation_service
from app.services.alert_service import compute_metric_value
from app.tasks import alert_tasks, policy_tasks
from tests.policy.fixtures import load_fixtures

VIOLATING = [f for f in load_fixtures() if f.expected]
BY_NAME = {f.name: f for f in VIOLATING}
CONFIRM = 'policy confirm_before_delete severity high:\n  always tool("delete_*") -> once event("confirmation")'
NO_DROP = 'policy no_drop severity critical:\n  always not tool("drop_db")'


def ev(seq, name, kind="tool", **kw):
    return {"seq": seq, "kind": kind, "name": name, **kw}


def fixture_events(fx):
    return [ev(e.seq, e.name, e.kind, status=e.status, attrs=dict(e.attrs)) for e in fx.events]


def _policy_row(org, source, action="log"):
    (p,) = parse(source)
    return TracePolicy(org_id=org.id, name=p.name, source=source, severity=p.severity, action=action)


def _rule(org, agent_id=None, severity="warning", **context):
    return AlertRule(org_id=org.id, agent_id=agent_id, name=f"pv-{severity}-{context}", metric="policy_violations",
                     operator=">=", threshold=1, window_minutes=15, severity=severity, context=context)


async def _add(db, *rows):
    db.add_all(rows)
    await db.commit()
    return rows


async def _all(db, model, *where):
    return (await db.execute(select(model).where(*where).execution_options(populate_existing=True))).scalars().all()


async def _messages(pubsub):
    out, idle = [], 0
    while idle < 2:  # a skipped subscribe confirmation also reads as None
        m = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.05)
        idle = idle + 1 if m is None else 0
        if m is not None:
            out.append(ViolationOut.model_validate_json(m["data"]))
    return out


@pytest.fixture
async def feed(redis, org):
    ps = redis.pubsub()
    await ps.subscribe(f"violations:{org.id}")
    yield ps
    await ps.aclose()


@pytest.mark.parametrize("fx", VIOLATING, ids=lambda f: f.name)
async def test_ingested_fixture_stores_publishes_and_alerts(client, db, org, agent, api_key_headers, enqueued,
                                                            feed, fx):
    await _add(db, *(_policy_row(org, pretty(p)) for p in parse(fx.source)), _rule(org))
    r = await client.post("/api/traces/ingest", headers=api_key_headers,
                          json={"agent_id": agent.id, "events": fixture_events(fx)})
    assert r.status_code == 202, r.text
    (trace_id,) = enqueued

    rows = await violation_service.check_and_publish(db, trace_id)
    expected = sorted((x.policy, x.at_seq, list(x.counterexample)) for x in fx.expected)
    assert sorted((r.policy_name, r.at_seq, r.counterexample) for r in rows) == expected
    stored = await _all(db, TracePolicyViolation)
    assert sorted((r.policy_name, r.at_seq, r.counterexample, r.mode) for r in stored) == [(*e, "batch") for e in expected]
    published = await _messages(feed)
    assert sorted((m.id, m.trace_id, m.at_seq, m.counterexample) for m in published) == sorted(
        (r.id, trace_id, r.at_seq, r.counterexample) for r in stored)
    (alert,) = await _all(db, AlertEvent)
    assert alert.value == len(expected) and sorted(alert.context["violation_ids"]) == sorted(r.id for r in stored)
    critical = any(r.severity == "critical" for r in stored)
    assert len(await _all(db, Incident)) == int(critical)

    assert await violation_service.check_and_publish(db, trace_id) == []
    assert await _messages(feed) == [] and len(await _all(db, AlertEvent)) == 1


def test_streamed_violation_reaches_ws_feed(app_, session_maker, org, agent, api_key, admin_user):
    fx = BY_NAME["no_exfil_violation"]

    async def setup():
        async with session_maker() as s:
            s.add_all([_policy_row(org, pretty(parse(fx.source)[0])), _rule(org, severity="critical")])
            await s.commit()
    asyncio.run(setup())

    token = make_access_token(admin_user.id, org.id, "admin")
    with TestClient(app_) as tc, tc.websocket_connect(f"/ws/traces?token={token}") as ws:
        r = tc.post("/api/traces/nx/events", headers={"X-API-Key": api_key},
                    json={"agent_id": agent.id, "events": fixture_events(fx)})
        assert r.status_code == 202, r.text
        (live,) = r.json()["violations"]
        msg = ws.receive_json()
        assert ViolationMessage.model_validate(msg).data.model_dump(mode="json") == live
        assert (live["policy_name"], live["at_seq"], live["mode"], live["counterexample"]) == (
            "no_exfil_after_secret", 4, "live", [])

    async def check():
        async with session_maker() as s:
            (row,) = (await s.execute(select(TracePolicyViolation))).scalars().all()
            (alert,) = (await s.execute(select(AlertEvent))).scalars().all()
            (inc,) = (await s.execute(select(Incident))).scalars().all()
            assert row.id == live["id"] and alert.context["violation_ids"] == [row.id]
            assert inc.severity == "critical" and inc.agent_id is None
    asyncio.run(check())


def test_ws_feed_still_forwards_traces(app_, org, agent, api_key, admin_user):
    token = make_access_token(admin_user.id, org.id, "admin")
    with TestClient(app_) as tc, tc.websocket_connect(f"/ws/traces?token={token}") as ws:
        r = tc.post("/api/traces/ingest", headers={"X-API-Key": api_key},
                    json={"agent_id": agent.id, "input": "hi", "tool_calls": [{"name": "a"}]})
        assert r.status_code == 202, r.text
        msg = ws.receive_json()
        assert msg["type"] == "trace" and msg["data"]["id"] == r.json()["id"]


async def test_batch_fills_live_counterexample_without_republishing(client, db, org, agent, api_key_headers,
                                                                    auth_headers, enqueued, feed):
    await _add(db, _policy_row(org, CONFIRM), _rule(org))
    r = await client.post("/api/traces/u1/events", headers=api_key_headers, json={
        "agent_id": agent.id, "events": [ev(0, "search"), ev(1, "delete_user")], "final": True})
    (live,) = r.json()["violations"]
    assert [m.id for m in await _messages(feed)] == [live["id"]]
    assert live["counterexample"] == []

    assert await violation_service.check_and_publish(db, enqueued[0]) == []
    (row,) = await _all(db, TracePolicyViolation)
    assert (row.id, row.mode, row.counterexample) == (live["id"], "live", [0, 1])
    assert await _messages(feed) == [] and len(await _all(db, AlertEvent)) == 1

    r = await client.get(f"/api/traces/{enqueued[0]}/violations", headers=auth_headers)
    assert [(v["id"], v["counterexample"]) for v in r.json()] == [(live["id"], [0, 1])]


async def _stream(client, headers, agent, uid, *names):
    r = await client.post(f"/api/traces/{uid}/events", headers=headers, json={
        "agent_id": agent.id, "events": [ev(i, n) for i, n in enumerate(names)]})
    assert r.status_code == 202, r.text
    return r.json()["violations"]


async def test_rule_filters_by_severity_and_agent(client, db, org, agent, api_key_headers, feed):
    other = (await _add(db, Agent(org_id=org.id, name="other")))[0]
    crit_only, other_agent, mine, high = await _add(
        db, _rule(org, severity_at_least="critical"), _rule(org, agent_id=other.id),
        _rule(org, agent_id=agent.id), _rule(org, severity_at_least="high"))
    await _add(db, _policy_row(org, CONFIRM))
    await _stream(client, api_key_headers, agent, "u1", "delete_user")
    fired = {a.rule_id for a in await _all(db, AlertEvent)}
    assert fired == {mine.id, high.id}
    assert await _all(db, Incident) == []

    await _add(db, _policy_row(org, NO_DROP))
    await _stream(client, api_key_headers, agent, "u2", "drop_db")
    alerts = await _all(db, AlertEvent)
    assert {a.rule_id for a in alerts} == {mine.id, high.id, crit_only.id}  # the others are deduplicated
    assert [a.value for a in alerts if a.rule_id == crit_only.id] == [1]
    (inc,) = await _all(db, Incident)
    assert inc.severity == "critical" and "breached" in inc.title and other_agent.id not in {a.rule_id for a in alerts}


async def test_threshold_counts_window(client, db, org, agent, api_key_headers, feed):
    (rule,) = await _add(db, AlertRule(org_id=org.id, name="3 in 15m", metric="policy_violations", operator=">=",
                                       threshold=3, window_minutes=15, dedup_minutes=0, context={}))
    await _add(db, _policy_row(org, NO_DROP))
    old = (await _add(db, Trace(org_id=org.id, agent_id=agent.id, trace_uid="old")))[0]
    p = (await _all(db, TracePolicy))[0]
    await _add(db, TracePolicyViolation(
        org_id=org.id, trace_id=old.id, agent_id=agent.id, policy_id=p.id, policy_name=p.name, policy_version=1,
        severity="critical", at_seq=0, mode="batch", detected_at=datetime.utcnow() - timedelta(minutes=30)))
    for uid in ("a", "b"):
        await _stream(client, api_key_headers, agent, uid, "drop_db")
    assert await _all(db, AlertEvent) == []
    await _stream(client, api_key_headers, agent, "c", "drop_db")
    (alert,) = await _all(db, AlertEvent)
    assert alert.value == 3 and alert.rule_id == rule.id


async def test_publish_failures_never_fail_ingest(client, db, org, agent, api_key_headers, redis, monkeypatch):
    async def boom(*a, **k):
        raise ConnectionError("redis down")

    async def boom_alerts(*a, **k):
        raise RuntimeError("alerts down")

    monkeypatch.setattr(redis, "pipeline", lambda **k: SimpleNamespace(publish=lambda *a: None, execute=boom))
    monkeypatch.setattr(violation_service, "evaluate_violation_alerts", boom_alerts)
    await _add(db, _policy_row(org, NO_DROP))
    (v,) = await _stream(client, api_key_headers, agent, "u1", "drop_db")
    assert [r.id for r in await _all(db, TracePolicyViolation)] == [v["id"]]


async def test_alert_failure_leaves_caller_session_usable(db, org, agent, monkeypatch):
    async def boom(s, rows):
        await s.execute(select(AlertRule))
        raise RuntimeError("alerts down")

    monkeypatch.setattr(violation_service, "evaluate_violation_alerts", boom)
    (p,) = await _add(db, _policy_row(org, NO_DROP))
    (t,) = await _add(db, Trace(org_id=org.id, agent_id=agent.id, trace_uid="t"))
    row = TracePolicyViolation(id="v1", org_id=org.id, trace_id=t.id, agent_id=agent.id, policy_id=p.id,
                               policy_name="no_drop", policy_version=1, severity="critical", at_seq=0,
                               counterexample=[0], mode="live", detected_at=datetime.utcnow())
    await violation_service.publish_violations(db, [row])
    assert t.id and t.trace_uid == "t"  # an expired instance would need a lazy load here and fail


async def test_alert_notifications_are_dispatched(client, db, org, agent, api_key_headers, monkeypatch):
    sent = []

    async def dispatch(channels, message, severity):
        sent.append((channels, message, severity))

    monkeypatch.setattr(violation_service, "dispatch_alert", dispatch)
    rule = _rule(org, severity="critical")
    rule.channels = [{"type": "slack", "target": "https://hooks.example/x"}]
    await _add(db, rule, _policy_row(org, NO_DROP))
    await _stream(client, api_key_headers, agent, "u1", "drop_db")
    await violation_service.drain_notifications()
    assert sent == [([{"type": "slack", "target": "https://hooks.example/x"}],
                     f"{rule.name}: policy_violations >= 1.0 (observed 1.0)", "critical")]


async def test_celery_task_checks_publishes_and_closes_redis(monkeypatch, session_maker, db, org, agent, redis):
    calls = []

    async def fake_check(s, trace_id, counterexamples):
        calls.append(trace_id)
        return []

    closed = []

    async def fake_close():
        closed.append(True)

    monkeypatch.setattr(policy_tasks, "check_and_publish", fake_check)
    monkeypatch.setattr(policy_tasks, "close_redis", fake_close)
    monkeypatch.setattr(policy_tasks, "async_sessionmaker", lambda *a, **k: session_maker)
    assert await asyncio.to_thread(policy_tasks.check_completed_trace.run, "t1") == 0
    assert calls == ["t1"] and closed == [True]


async def test_celery_task_past_soft_limit_stores_verdicts(monkeypatch, session_maker, caplog):
    calls = []

    async def fake_check(s, trace_id, counterexamples):
        calls.append(counterexamples)
        if counterexamples:
            raise SoftTimeLimitExceeded()
        return ["row"]

    async def fake_close():
        pass

    monkeypatch.setattr(policy_tasks, "check_and_publish", fake_check)
    monkeypatch.setattr(policy_tasks, "close_redis", fake_close)
    monkeypatch.setattr(policy_tasks, "async_sessionmaker", lambda *a, **k: session_maker)
    assert await asyncio.to_thread(policy_tasks.check_completed_trace.run, "t1") == 1
    assert calls == [True, False] and "soft time limit" in caplog.text


def test_policy_checks_have_their_own_queue():
    route = celery.amqp.router.route
    assert route({}, policy_tasks.check_completed_trace.name)["queue"].name == "policy"
    assert route({}, alert_tasks.evaluate_alert_rules.name)["queue"].name == "celery"
    assert (policy_tasks.check_completed_trace.soft_time_limit, policy_tasks.check_completed_trace.time_limit) == \
        (240, 300)


async def test_metric_counts_window_severity_and_agent(db, org, agent):
    t = (await _add(db, Trace(org_id=org.id, agent_id=agent.id, trace_uid="t")))[0]
    (p,) = await _add(db, TracePolicy(org_id=org.id, name="p", source="x", severity="high"))
    now = datetime.utcnow()
    await _add(db, *(TracePolicyViolation(org_id=org.id, trace_id=t.id, agent_id=agent.id, policy_id=p.id,
                                          policy_name="p", policy_version=version, severity=sev, at_seq=0,
                                          mode="batch", detected_at=now - timedelta(minutes=ago))
                     for version, (sev, ago) in enumerate([("high", 1), ("low", 1), ("critical", 2), ("critical", 60)])))
    rule = lambda **kw: AlertRule(org_id=org.id, metric="policy_violations", window_minutes=15, **kw)  # noqa: E731
    assert await compute_metric_value(db, rule(context={})) == 3
    assert await compute_metric_value(db, rule(context={"severity_at_least": "high"})) == 2
    assert await compute_metric_value(db, rule(context={"severity_at_least": "critical"}, agent_id=agent.id)) == 1
    assert await compute_metric_value(db, rule(context={}, agent_id="nobody")) == 0


def test_periodic_alert_task_leaves_policy_violation_rules_to_ingest(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path}/a.db")
    Base.metadata.create_all(eng)
    Session = sessionmaker(eng)
    with Session() as s:
        o = Organization(name="o", slug="o")
        s.add(o)
        s.flush()
        s.add(AlertRule(org_id=o.id, name="pv", metric="policy_violations", operator=">=", threshold=0))
        s.commit()
    monkeypatch.setattr(alert_tasks, "SyncSession", Session)
    assert alert_tasks.evaluate_alert_rules.run() == {"checked": 0, "fired": 0}
    eng.dispose()


async def test_trace_violations_endpoint(client, db, org, agent, api_key_headers, auth_headers):
    await _add(db, _policy_row(org, NO_DROP), _policy_row(org, CONFIRM))
    await _stream(client, api_key_headers, agent, "u1", "search", "drop_db", "delete_user")
    (trace,) = await _all(db, Trace)
    r = await client.get(f"/api/traces/{trace.id}/violations", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert [(v["policy_name"], v["at_seq"], v["trace_id"], v["mode"]) for v in r.json()] == [
        ("no_drop", 1, trace.id, "live"), ("confirm_before_delete", 2, trace.id, "live")]

    other_org = (await _add(db, Organization(name="Other", slug="other")))[0]
    stranger = (await _add(db, User(email="s@example.com", password_hash="x", full_name="s")))[0]
    await _add(db, OrgMembership(org_id=other_org.id, user_id=stranger.id, role="admin"))
    token = make_access_token(stranger.id, other_org.id, "admin")
    r = await client.get(f"/api/traces/{trace.id}/violations", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 404
    assert (await client.get("/api/traces/missing/violations", headers=auth_headers)).status_code == 404
    other = (await _add(db, Trace(org_id=org.id, agent_id=agent.id, trace_uid="clean")))[0]
    assert (await client.get(f"/api/traces/{other.id}/violations", headers=auth_headers)).json() == []


async def test_alert_rule_api_accepts_severity_floor(client, auth_headers):
    body = {"name": "pv", "metric": "policy_violations", "operator": ">=", "threshold": 1}
    r = await client.post("/api/alerts/rules", headers=auth_headers,
                          json={**body, "context": {"severity_at_least": "high"}})
    assert r.status_code == 200, r.text
    assert r.json()["context"] == {"severity_at_least": "high"}
    rid = r.json()["id"]
    r = await client.patch(f"/api/alerts/rules/{rid}", headers=auth_headers,
                           json={"context": {"severity_at_least": "critical"}})
    assert r.json()["context"] == {"severity_at_least": "critical"}
    assert (await client.post("/api/alerts/rules", headers=auth_headers, json=body)).json()["context"] == {}
    r = await client.post("/api/alerts/rules", headers=auth_headers,
                          json={**body, "context": {"severity_at_least": "urgent"}})
    assert r.status_code == 422
