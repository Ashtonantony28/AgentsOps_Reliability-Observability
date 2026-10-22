"""SG-AP1.1: one org's alert channels or eval runs don't hold up another org's alerts (AUDIT-15 A15-1, A15-10)."""
import asyncio
import re
import time
from pathlib import Path

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.celery_app import celery
from app.models import (
    Agent, AlertEvent, AlertRule, EvaluationCase, EvaluationResult, EvaluationRun, EvaluationSet, Organization,
)
from app.schemas.alert import MAX_CHANNELS
from app.services import alert_service, evaluation_service, notification_service
from app.services.alert_service import evaluate_metric_rules
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
