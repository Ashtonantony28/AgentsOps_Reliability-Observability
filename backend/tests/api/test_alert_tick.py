"""SG-AP1.1: one org's alert channels or eval runs don't hold up another org's alerts (AUDIT-15 A15-1, A15-10)."""
import asyncio
import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.celery_app import celery
from app.middleware.auth import make_access_token
from app.models import (
    Agent, AlertEvent, AlertRule, EvaluationCase, EvaluationResult, EvaluationRun, EvaluationSet, Organization,
    OrgMembership, Trace,
)
from app.schemas.alert import MAX_CHANNELS, MAX_ORG_RULES, MAX_WINDOW_MINUTES
from app.services import alert_service, evaluation_service, notification_service
from app.services.alert_service import compute_metric_value, evaluate_metric_rules
from app.services.notification_service import dispatch_alert
from app.tasks import alert_tasks, evaluation_tasks

ROOT = Path(__file__).resolve().parents[3]


async def _add(db, *rows):
    db.add_all(rows)
    await db.commit()
    return rows


def _firing(org, name, channels=1):
    return AlertRule(org_id=org.id, name=name, metric="token_usage", operator=">=", threshold=0,
                     channels=[{"type": "webhook", "target": f"https://{name}.example/{i}"} for i in range(channels)])


async def _count(session_maker, model):
    async with session_maker() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


@pytest.fixture
def dispatch(monkeypatch):
    """Replaces the tick's dispatch: `hold[name]` (an Event) keeps a rule's page open until set; records starts/ends."""
    started, done, hold = [], [], {}

    async def fake(channels, message, severity):
        name = message.split(":")[0]
        started.append(name)
        if name in hold:
            await hold[name].wait()
        done.append(name)
        return [True] * len(channels)

    monkeypatch.setattr(alert_service, "dispatch_alert", fake)
    return started, done, hold


# --- the tick: every rule evaluated before any dispatch, dispatches concurrent, fair by org, under a deadline ---

async def test_every_rule_is_evaluated_before_any_dispatch(db, session_maker, org, monkeypatch):
    other = (await _add(db, Organization(name="B", slug="b")))[0]
    await _add(db, _firing(org, "a"), _firing(other, "b"))
    seen = []

    async def record(channels, message, severity):
        seen.append(await _count(session_maker, AlertEvent))
        return [True]

    monkeypatch.setattr(alert_service, "dispatch_alert", record)
    assert await evaluate_metric_rules(db) == {"checked": 2, "fired": 2}
    assert seen == [2, 2]


async def test_dispatches_run_concurrently(db, org, dispatch):
    started, done, hold = dispatch
    hold["a"], hold["b"] = asyncio.Event(), asyncio.Event()
    await _add(db, _firing(org, "a"), _firing(org, "b"))
    task = asyncio.create_task(evaluate_metric_rules(db))
    for _ in range(200):
        if len(started) == 2:
            break
        await asyncio.sleep(0.01)
    assert sorted(started) == ["a", "b"] and done == []  # both pages open at once
    hold["b"].set()
    hold["a"].set()
    assert await asyncio.wait_for(task, 5) == {"checked": 2, "fired": 2}


async def test_dispatch_turns_alternate_between_orgs(db, org, dispatch, monkeypatch):
    started, _, _ = dispatch
    monkeypatch.setattr(alert_service, "DISPATCH_CONCURRENCY", 1)
    other = (await _add(db, Organization(name="B", slug="b")))[0]
    for name in ("a1", "a2", "a3"):  # org A's rules are older
        await _add(db, _firing(org, name))
    await _add(db, _firing(other, "b1"))
    assert await evaluate_metric_rules(db) == {"checked": 4, "fired": 4}
    assert started == ["a1", "b1", "a2", "a3"]


async def test_dispatch_stops_at_the_ticks_deadline(db, org, dispatch, monkeypatch, caplog):
    started, done, hold = dispatch
    monkeypatch.setattr(alert_service, "DISPATCH_DEADLINE_S", 0.2)
    hold["slow"] = asyncio.Event()  # never set
    await _add(db, _firing(org, "slow"), _firing(org, "fast"))
    t0 = time.monotonic()
    assert await asyncio.wait_for(evaluate_metric_rules(db), 5) == {"checked": 2, "fired": 2}
    assert time.monotonic() - t0 < 2
    assert sorted(started) == ["fast", "slow"] and done == ["fast"]
    assert "1 alert dispatch" in caplog.text


async def test_rules_without_channels_dispatch_nothing(db, org, dispatch):
    started, _, _ = dispatch
    await _add(db, _firing(org, "quiet", channels=0))
    assert await evaluate_metric_rules(db) == {"checked": 1, "fired": 1}
    assert started == []


async def test_a_stored_dedup_below_one_minute_dedups_as_the_default(db, org, dispatch):
    started, _, _ = dispatch
    for minutes in (-5, 0):
        rule = _firing(org, f"d{minutes}")
        rule.dedup_minutes = minutes
        await _add(db, rule)
    assert await evaluate_metric_rules(db) == {"checked": 2, "fired": 2}
    assert await evaluate_metric_rules(db) == {"checked": 2, "fired": 0}
    assert len(started) == 2


# --- dispatch_alert: a bounded number of channels, sent concurrently ---

async def test_dispatch_alert_sends_at_most_the_bound_concurrently(monkeypatch):
    sent, release = [], asyncio.Event()

    async def send(url, body):
        sent.append(url)
        await release.wait()
        return True

    monkeypatch.setattr(notification_service, "send_webhook", send)
    channels = [{"type": "webhook", "target": f"https://h.example/{i}"} for i in range(MAX_CHANNELS + 5)]
    task = asyncio.create_task(dispatch_alert(channels, "m", "warning"))
    for _ in range(200):
        if len(sent) == MAX_CHANNELS:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    assert sent == [c["target"] for c in channels[:MAX_CHANNELS]]  # all open at once, none past the bound
    release.set()
    assert await asyncio.wait_for(task, 5) == [True] * MAX_CHANNELS


# --- the API: channels per rule bounded, dedup_minutes >= 1 ---

def _chans(n):
    return [{"type": "webhook", "target": f"https://h.example/{i}", "name": ""} for i in range(n)]


@pytest.mark.parametrize("field, bad, good", [("dedup_minutes", 0, 1), ("dedup_minutes", -5, 1),
                                              ("channels", _chans(MAX_CHANNELS + 1), _chans(MAX_CHANNELS))])
async def test_rule_api_bounds_channels_and_dedup(client, auth_headers, field, bad, good):
    body = {"name": "r", "metric": "error_rate", "operator": ">", "threshold": 0.5}
    assert (await client.post("/api/alerts/rules", headers=auth_headers, json={**body, field: bad})).status_code == 422
    r = await client.post("/api/alerts/rules", headers=auth_headers, json={**body, field: good})
    assert r.status_code == 200, r.text
    assert r.json()[field] == good
    rid = r.json()["id"]
    r = await client.patch(f"/api/alerts/rules/{rid}", headers=auth_headers, json={field: bad})
    assert r.status_code == 422
    r = await client.patch(f"/api/alerts/rules/{rid}", headers=auth_headers, json={field: good})
    assert r.status_code == 200 and r.json()[field] == good


# --- eval runs: their own queue, results committed per case, a run past its time limit marked failed ---

def _route(name):
    return celery.amqp.router.route({}, name)["queue"].name


def test_eval_runs_have_their_own_queue_and_time_limits():
    assert _route(evaluation_tasks.execute_run.name) == "evaluations"
    assert _route(alert_tasks.evaluate_alert_rules.name) == "celery"
    assert _route(evaluation_tasks.run_scheduled_evaluations.name) == "celery"  # it only queues runs
    assert (evaluation_tasks.execute_run.soft_time_limit, evaluation_tasks.execute_run.time_limit) == (240, 300)


@pytest.mark.parametrize("compose", ["docker-compose.yml", "docker-compose.prod.yml"])
def test_every_routed_queue_has_a_worker(compose):
    queues = {q for cmd in re.findall(r"worker -Q (\S+)", (ROOT / compose).read_text()) for q in cmd.split(",")}
    assert {r["queue"] for r in celery.conf.task_routes.values()} | {"celery"} <= queues


@pytest.fixture
async def eval_run(db, org):
    agent = (await _add(db, Agent(org_id=org.id, name="ag")))[0]
    es = (await _add(db, EvaluationSet(org_id=org.id, name="s", evaluators=["exact"],
                                       runner={"type": "webhook", "url": "https://r.example/run"})))[0]
    await _add(db, *(EvaluationCase(set_id=es.id, input=f"q{i}", expected_output="ok") for i in range(3)))
    return (await _add(db, EvaluationRun(set_id=es.id, agent_id=agent.id, triggered_by="scheduled")))[0]


@pytest.fixture
def worker(monkeypatch, engine, session_maker):
    """execute_run's sessions on the test database; case outputs from `outputs` (an exception is raised)."""
    eng = create_engine(engine.url.set(drivername=engine.url.get_backend_name()), poolclass=NullPool)
    monkeypatch.setattr(evaluation_tasks, "SyncSession", sessionmaker(eng, expire_on_commit=False))
    monkeypatch.setattr(evaluation_tasks, "async_sessionmaker", lambda *a, **k: session_maker)
    outputs = []

    async def case_output(db, eval_set, run, case):
        out = outputs.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out, "", 1

    monkeypatch.setattr(evaluation_service, "case_output", case_output)
    yield outputs
    eng.dispose()


async def _run_state(session_maker, run_id):
    async with session_maker() as s:
        run = await s.get(EvaluationRun, run_id)
        results = (await s.execute(select(EvaluationResult).where(EvaluationResult.run_id == run_id))).scalars().all()
        return run, results


async def test_a_run_past_its_soft_time_limit_is_failed_with_the_cases_it_scored(eval_run, worker, session_maker,
                                                                                 caplog):
    worker.extend(["ok", "nope", SoftTimeLimitExceeded()])
    assert await asyncio.to_thread(evaluation_tasks.execute_run.run, eval_run.id) == eval_run.id
    run, results = await _run_state(session_maker, eval_run.id)
    assert (run.status, run.passed, run.failed, run.total, run.summary.get("error")) == (
        "failed", 1, 1, 2, "time_limit")
    assert run.completed_at is not None and sorted(r.passed for r in results) == [False, True]
    assert "time limit" in caplog.text


async def test_a_run_that_errors_is_failed_and_the_error_raised(eval_run, worker, session_maker):
    worker.extend(["ok", RuntimeError("boom")])
    with pytest.raises(RuntimeError):
        await asyncio.to_thread(evaluation_tasks.execute_run.run, eval_run.id)
    run, results = await _run_state(session_maker, eval_run.id)
    assert (run.status, run.total, run.summary.get("error")) == ("failed", 1, "RuntimeError") and len(results) == 1


async def test_each_case_result_is_committed_as_it_is_scored(eval_run, worker, session_maker, monkeypatch):
    committed = []
    original = evaluation_service.case_output  # the worker fixture's

    async def watch(db, eval_set, run, case):
        committed.append(await _count(session_maker, EvaluationResult))
        return await original(db, eval_set, run, case)

    monkeypatch.setattr(evaluation_service, "case_output", watch)
    worker.extend(["ok", "ok", "nope"])
    await asyncio.to_thread(evaluation_tasks.execute_run.run, eval_run.id)
    run, results = await _run_state(session_maker, eval_run.id)
    assert committed == [0, 1, 2]
    assert (run.status, run.passed, run.failed, run.total) == ("completed", 2, 1, 3) and len(results) == 3


async def test_a_finished_run_is_not_failed_afterwards(eval_run, worker, session_maker):
    worker.extend(["ok", "ok", "ok"])
    await asyncio.to_thread(evaluation_tasks.execute_run.run, eval_run.id)
    evaluation_tasks.fail_run(eval_run.id, "time_limit")
    run, _ = await _run_state(session_maker, eval_run.id)
    assert (run.status, run.passed) == ("completed", 3)


# --- SG-AP2.1: a rule that raises is skipped, fired pages go out however checks end, windows bounded, SQL aggregates ---

@pytest.fixture
def failing(monkeypatch):
    """`failing[name]` is an async callable run instead of that rule's check."""
    real, failing = alert_service.compute_metric_value, {}

    async def compute(db, rule):
        if rule.name in failing:
            await failing[rule.name](db)
        return await real(db, rule)

    monkeypatch.setattr(alert_service, "compute_metric_value", compute)
    return failing


async def _overflow(db):
    raise OverflowError("date value out of range")


async def _bad_query(db):
    await db.execute(text("SELECT * FROM no_such_table"))


@pytest.mark.parametrize("fail", [_overflow, _bad_query])
async def test_a_rule_whose_check_raises_is_logged_and_skipped(db, session_maker, org, dispatch, failing, caplog, fail):
    started, _, _ = dispatch
    for name in ("a", "bad", "c"):
        await _add(db, _firing(org, name))
    failing["bad"] = fail
    assert await evaluate_metric_rules(db) == {"checked": 3, "fired": 2}
    assert sorted(started) == ["a", "c"]
    assert await _count(session_maker, AlertEvent) == 2
    assert "check failed, skipped" in caplog.text


async def test_pages_fired_are_sent_when_the_check_phase_raises(db, org, dispatch, failing, monkeypatch):
    started, _, _ = dispatch
    for name in ("a", "bad", "never"):
        await _add(db, _firing(org, name))
    failing["bad"] = _overflow

    async def rollback():
        raise ConnectionError("database gone")

    monkeypatch.setattr(db, "rollback", rollback)
    with pytest.raises(ConnectionError):
        await evaluate_metric_rules(db)
    assert started == ["a"]


async def test_checks_stop_at_their_deadline_and_the_pages_fired_go_out(db, org, dispatch, failing, monkeypatch,
                                                                       caplog):
    started, _, _ = dispatch
    assert alert_service.CHECK_DEADLINE_S + alert_service.DISPATCH_DEADLINE_S < celery.conf.task_time_limit
    monkeypatch.setattr(alert_service, "CHECK_DEADLINE_S", 0.2)
    for name in ("a", "stuck", "never"):
        await _add(db, _firing(org, name))
    failing["stuck"] = lambda db: asyncio.Event().wait()
    t0 = time.monotonic()
    assert await asyncio.wait_for(evaluate_metric_rules(db), 5) == {"checked": 2, "fired": 1}
    assert time.monotonic() - t0 < 2
    assert started == ["a"]
    assert "alert checks stopped at the 0.2s deadline after 2 rule(s)" in caplog.text


async def test_no_more_pages_are_sent_at_once_than_the_bound(db, org, dispatch):
    started, done, hold = dispatch
    names = [f"r{i}" for i in range(alert_service.DISPATCH_CONCURRENCY + 1)]
    release = asyncio.Event()
    hold.update(dict.fromkeys(names, release))
    await _add(db, *(_firing(org, n) for n in names))
    task = asyncio.create_task(evaluate_metric_rules(db))
    for _ in range(200):
        if len(started) == alert_service.DISPATCH_CONCURRENCY:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    assert len(started) == alert_service.DISPATCH_CONCURRENCY and done == []
    release.set()
    assert await asyncio.wait_for(task, 5) == {"checked": len(names), "fired": len(names)}
    assert sorted(done) == sorted(names)


@pytest.mark.parametrize("bad", [0, -5, MAX_WINDOW_MINUTES + 1, 2_000_000_000])
async def test_rule_api_bounds_the_window(client, auth_headers, bad):
    body = {"name": "r", "metric": "error_rate", "operator": ">", "threshold": 0.5}
    assert (await client.post("/api/alerts/rules", headers=auth_headers,
                              json={**body, "window_minutes": bad})).status_code == 422
    for good in (1, MAX_WINDOW_MINUTES):
        r = await client.post("/api/alerts/rules", headers=auth_headers, json={**body, "window_minutes": good})
        assert r.status_code == 200 and r.json()["window_minutes"] == good, r.text
    rid = r.json()["id"]
    r = await client.patch(f"/api/alerts/rules/{rid}", headers=auth_headers, json={"window_minutes": bad})
    assert r.status_code == 422
    r = await client.patch(f"/api/alerts/rules/{rid}", headers=auth_headers, json={"window_minutes": 1})
    assert r.status_code == 200 and r.json()["window_minutes"] == 1


def _trace(agent, minutes_ago, **kw):
    return Trace(org_id=agent.org_id, agent_id=agent.id, trace_uid=f"t-{random.random()}",
                 created_at=datetime.utcnow() - timedelta(minutes=minutes_ago), **kw)


@pytest.mark.parametrize("stored", [2_000_000_000, MAX_WINDOW_MINUTES + 1, 0, -5, None])
async def test_a_stored_window_outside_the_bound_is_checked_with_the_default(db, org, agent, stored):
    await _add(db, _trace(agent, 10, tokens_input=2, tokens_output=3), _trace(agent, 20, tokens_input=100))
    rule = AlertRule(org_id=org.id, name="w", metric="token_usage", operator=">=", threshold=0, window_minutes=stored)
    assert await compute_metric_value(db, rule) == 5  # the default 15 minutes
    rule.window_minutes = MAX_WINDOW_MINUTES
    assert await compute_metric_value(db, rule) == 105


@pytest.mark.parametrize("n", [0, 1, 2, 7, 40])
async def test_metric_values_match_the_rows_they_aggregate(db, org, agent, n):
    rng = random.Random(n)
    rows = [_trace(agent, rng.randint(0, 13), status=rng.choice(["success", "success", "error", "timeout"]),
                   latency_ms=rng.randint(0, 5000), tokens_input=rng.randint(0, 500), tokens_output=rng.randint(0, 500))
            for _ in range(n)]
    sibling = (await _add(db, Agent(org_id=org.id, name="sibling")))[0]
    other = (await _add(db, Organization(name="B", slug="b")))[0]
    stranger = (await _add(db, Agent(org_id=other.id, name="stranger")))[0]
    noise = [_trace(a, m, status="error", latency_ms=99_999, tokens_input=7)
             for a, m in ((agent, 30), (sibling, 1), (stranger, 1))]  # out of window, agent and org
    await _add(db, *rows, *noise)
    lat = [r.latency_ms for r in rows]
    expected = {
        "error_rate": round(sum(r.status != "success" for r in rows) / n, 4) if n else 0.0,
        "latency_p95": float(np.percentile(lat, 95)) if n else 0.0,
        "latency_avg": float(np.mean(lat)) if n else 0.0,
        "token_usage": float(sum(r.tokens_input + r.tokens_output for r in rows)),
    }
    for metric, value in expected.items():
        rule = AlertRule(org_id=org.id, agent_id=agent.id, name=metric, metric=metric, operator=">", threshold=0,
                         window_minutes=15)
        assert await compute_metric_value(db, rule) == pytest.approx(value), metric
    org_wide = AlertRule(org_id=org.id, name="t", metric="token_usage", operator=">", threshold=0, window_minutes=15)
    assert await compute_metric_value(db, org_wide) == expected["token_usage"] + 7


async def test_metric_checks_do_not_load_trace_rows(db, engine, org, agent):
    await _add(db, _trace(agent, 1, input="x" * 1000, output="y" * 1000, latency_ms=5))
    statements = []

    def record(conn, cursor, statement, *a):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        for metric in ("error_rate", "latency_p95", "latency_avg", "token_usage"):
            rule = AlertRule(org_id=org.id, name=metric, metric=metric, operator=">", threshold=0, window_minutes=15)
            await compute_metric_value(db, rule)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
    assert statements and not [s for s in statements if "traces.input" in s or "traces.output" in s]



# --- SG-AP3.1: rules checked in turns by org from where the last tick stopped, rules per org bounded, fired once ---

@pytest.fixture
def checked(monkeypatch):
    """Names of the rules whose checks started, in order; `hooks[name]` is an async callable awaited first."""
    real, names, hooks = alert_service.compute_metric_value, [], {}

    async def compute(db, rule):
        names.append(rule.name)
        if rule.name in hooks:
            await hooks[rule.name](db)
        return await real(db, rule)

    monkeypatch.setattr(alert_service, "compute_metric_value", compute)
    return names, hooks


async def test_rules_are_checked_in_turns_by_org(db, org, dispatch, checked):
    names, _ = checked
    b, c = await _add(db, Organization(name="B", slug="b"), Organization(name="C", slug="c"))
    for rule in (_firing(org, "a1"), _firing(org, "a2"), _firing(org, "a3"), _firing(c, "c1"), _firing(b, "b1"),
                 _firing(c, "c2")):
        await _add(db, rule)
    assert await evaluate_metric_rules(db) == {"checked": 6, "fired": 6}
    assert names == ["a1", "c1", "b1", "a2", "c2", "a3"]  # orgs by their oldest rule


async def test_a_tick_carries_on_from_the_rule_the_last_tick_did_not_reach(db, org, dispatch, checked, monkeypatch,
                                                                           caplog):
    names, hooks = checked
    monkeypatch.setattr(alert_service, "CHECK_DEADLINE_S", 0.2)
    for name in ("r0", "r1", "r2", "r3"):
        await _add(db, _firing(org, name))
    stuck = asyncio.Event()
    hooks["r1"] = lambda db: stuck.wait()
    assert await evaluate_metric_rules(db) == {"checked": 2, "fired": 1}
    assert "after 2 rule(s), 2 not reached" in caplog.text
    stuck.set()
    assert await evaluate_metric_rules(db) == {"checked": 4, "fired": 3}  # r0 is still firing
    assert await evaluate_metric_rules(db) == {"checked": 4, "fired": 0}
    assert names == ["r0", "r1", "r2", "r3", "r0", "r1", "r0", "r1", "r2", "r3"]  # a whole tick starts from the first


async def test_a_tick_carries_on_past_a_deleted_rule_it_did_not_reach(db, org, dispatch, checked, monkeypatch):
    names, hooks = checked
    monkeypatch.setattr(alert_service, "CHECK_DEADLINE_S", 0.2)
    other = (await _add(db, Organization(name="B", slug="b")))[0]
    rules = [_firing(org, "a0"), _firing(other, "b0"), _firing(org, "a1"), _firing(other, "b1")]
    for rule in rules:
        await _add(db, rule)
    hooks["b0"] = lambda db: asyncio.Event().wait()
    await evaluate_metric_rules(db)
    del hooks["b0"]
    await db.delete(rules[2])  # a1, the first rule not reached
    await db.commit()
    await evaluate_metric_rules(db)
    assert names == ["a0", "b0", "b1", "a0", "b0"]


async def test_another_orgs_costly_rules_delay_a_rule_by_a_bounded_number_of_ticks(db, session_maker, org, dispatch,
                                                                                   checked, monkeypatch):
    names, hooks = checked
    monkeypatch.setattr(alert_service, "CHECK_DEADLINE_S", 0.25)
    other = (await _add(db, Organization(name="B", slug="b")))[0]
    for i in range(12):  # each check takes 0.1 s, so a tick checks three rules
        await _add(db, _firing(org, f"a{i}"))
        hooks[f"a{i}"] = lambda db: asyncio.sleep(0.1)
    await _add(db, _firing(other, "b0"), _firing(other, "b1"))
    ticks = []
    for _ in range(6):
        async with session_maker() as s:  # a tick stopped mid-query leaves its session unusable, as in the worker
            await evaluate_metric_rules(s)
        ticks.append(len(names))
    assert "b0" in names[:ticks[0]] and "b1" in names[:ticks[1]]
    assert {f"a{i}" for i in range(12)} <= set(names)  # within ceil(14 / 3) + 1 ticks


async def test_overlapping_ticks_fire_a_rule_once(db, session_maker, org, dispatch, checked):
    started, _, _ = dispatch
    names, hooks = checked
    await _add(db, _firing(org, "a"))
    both = asyncio.Event()

    async def meet(db):
        if names.count("a") == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 5)

    hooks["a"] = meet

    async def tick():
        async with session_maker() as s:
            return await evaluate_metric_rules(s)

    summaries = await asyncio.gather(tick(), tick())
    assert sorted(s["fired"] for s in summaries) == [0, 1] and [s["checked"] for s in summaries] == [1, 1]
    assert await _count(session_maker, AlertEvent) == 1 and started == ["a"]


async def _member_headers(db, org, user):
    db.add(OrgMembership(org_id=org.id, user_id=user.id, role="admin"))
    await db.commit()
    return {"Authorization": f"Bearer {make_access_token(user.id, org.id, 'admin')}"}


async def test_an_org_has_at_most_max_org_rules(client, db, session_maker, org, admin_user, auth_headers):
    body = {"name": "r", "metric": "error_rate", "operator": ">", "threshold": 0.5}
    await _add(db, *(AlertRule(org_id=org.id, name=f"s{i}", metric="policy_violations", operator=">=", threshold=1,
                               enabled=i % 2 == 0) for i in range(MAX_ORG_RULES - 1)))  # disabled ones count too
    r = await client.post("/api/alerts/rules", headers=auth_headers, json=body)
    assert r.status_code == 200, r.text
    last = r.json()["id"]
    r = await client.post("/api/alerts/rules", headers=auth_headers, json=body)
    assert r.status_code == 409 and r.json()["error"]["code"] == "rule_limit"
    assert r.json()["error"]["details"]["max_rules"] == MAX_ORG_RULES
    assert await _count(session_maker, AlertRule) == MAX_ORG_RULES
    other = (await _add(db, Organization(name="B", slug="b")))[0]
    r = await client.post("/api/alerts/rules", headers=await _member_headers(db, other, admin_user), json=body)
    assert r.status_code == 200  # the bound is per org
    assert (await client.delete(f"/api/alerts/rules/{last}", headers=auth_headers)).status_code == 200
    assert (await client.post("/api/alerts/rules", headers=auth_headers, json=body)).status_code == 200
