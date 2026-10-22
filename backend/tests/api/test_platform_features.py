"""SG-P2: the platform does what the README says (eval runner, scheduled evals, alerts, retention, integrations)."""
import asyncio
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.celery_app import celery
from app.database import Base
from app.models import (
    Agent, AlertEvent, AlertRule, EvaluationCase, EvaluationResult, EvaluationRun, EvaluationSet, Incident,
    Integration, Organization, Span, Trace, TracePolicy, TracePolicyViolation,
)
from app.services import alert_service, cron, outbound
from app.services.alert_service import compute_metric_value, evaluate_metric_rules
from app.tasks import alert_tasks, evaluation_tasks, retention_tasks

README = (Path(__file__).resolve().parents[3] / "README.md").read_text()
PUBLIC_IP = "93.184.216.34"


async def _add(db, *rows):
    db.add_all(rows)
    await db.commit()
    return rows


async def _all(db, model, *where):
    return (await db.execute(select(model).where(*where).execution_options(populate_existing=True))).scalars().all()


async def _run(client, headers, agent, cases, runner=None, evaluators=("exact",)):
    body = {"name": "s", "evaluators": list(evaluators), "cases": cases, "runner": runner}
    r = await client.post("/api/evaluations/sets", json=body, headers=headers)
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    r = await client.post(f"/api/evaluations/sets/{sid}/run", json={"agent_id": agent.id}, headers=headers)
    assert r.status_code == 200, r.text
    run = r.json()
    detail = (await client.get(f"/api/evaluations/runs/{run['id']}", headers=headers)).json()
    case_ids = {c["id"]: c["input"] for c in (await client.get(f"/api/evaluations/sets/{sid}", headers=headers)).json()["cases"]}
    return run, {case_ids[x["case_id"]]: x for x in detail["results"]}


@pytest.fixture
def hook(monkeypatch):
    """The webhook runner's server: replies by case input; records the requests."""
    sent, replies = [], {}

    async def resolve(host, port):
        return [PUBLIC_IP]

    async def handler(request):
        body = json.loads(request.content)
        sent.append((request, body))
        r = replies.get(body["input"], (200, {"output": f"echo {body['input']}"}))
        if callable(r):
            return await r()
        status, payload = r
        return httpx.Response(status, text=payload) if isinstance(payload, str) else httpx.Response(status, json=payload)

    monkeypatch.setattr(outbound, "_resolve", resolve)
    monkeypatch.setattr(outbound, "_transport", httpx.MockTransport(handler))
    return sent, replies


# --- the eval runner: a case's output comes from the runner, and a case with none fails ---

async def test_no_runner_fails_every_case_even_when_expected_would_pass(client, db, auth_headers, agent, org):
    await _add(db, Trace(org_id=org.id, agent_id=agent.id, trace_uid="t", input="q1", output="a1"))
    run, results = await _run(client, auth_headers, agent, [{"input": "q1", "expected_output": "a1"},
                                                            {"input": "q2", "expected_output": ""}],
                              evaluators=("exact", "semantic"))
    assert (run["passed"], run["failed"], run["total"], run["score"], run["status"]) == (0, 2, 2, 0.0, "completed")
    for r in results.values():
        assert (r["passed"], r["score"], r["reason"], r["actual_output"], r["scores_by_evaluator"]) == \
            (False, 0.0, "no_runner", "", {})


async def test_webhook_runner_posts_each_case_and_scores_its_output(client, db, auth_headers, agent, hook):
    sent, replies = hook
    replies["right"] = (200, {"output": "the answer"})
    replies["wrong"] = (200, {"output": "not it", "extra": 1})
    url = "https://runner.example.com/case?x=1"
    run, results = await _run(client, auth_headers, agent, [{"input": "right", "expected_output": "the answer"},
                                                            {"input": "wrong", "expected_output": "the answer"}],
                              runner={"type": "webhook", "url": url})
    assert (run["passed"], run["failed"]) == (1, 1)
    assert results["right"]["passed"] and results["right"]["actual_output"] == "the answer"
    assert results["right"]["scores_by_evaluator"] == {"exact": 1.0}
    assert not results["wrong"]["passed"] and results["wrong"]["actual_output"] == "not it"
    assert {b["input"] for _, b in sent} == {"right", "wrong"}
    request, body = next(x for x in sent if x[1]["input"] == "right")
    assert body == {"run_id": run["id"], "set_id": run["set_id"], "case_id": results["right"]["case_id"],
                    "agent_id": agent.id, "agent_version": agent.current_version, "input": "right"}
    assert request.method == "POST" and request.url.host == PUBLIC_IP and request.headers["host"] == "runner.example.com"
    assert request.url.query == b"x=1"


@pytest.mark.parametrize("reply, reason", [
    ((500, {"output": "x"}), "runner_error: status 500"),
    ((404, "nope"), "runner_error: status 404"),
    ((200, "not json"), "runner_error: invalid_json"),
    ((200, ["output"]), "runner_error: no output string"),
    ((200, {"output": 5}), "runner_error: no output string"),
    ((200, {"answer": "x"}), "runner_error: no output string"),
    ((200, {"output": ""}), "no_output"),
    ((200, {"output": "  \n"}), "no_output"),
])
async def test_webhook_runner_failures_fail_the_case(client, auth_headers, agent, hook, reply, reason):
    hook[1]["q"] = reply
    run, results = await _run(client, auth_headers, agent, [{"input": "q", "expected_output": ""}],
                              runner={"type": "webhook", "url": "https://runner.example.com/"})
    assert (results["q"]["passed"], results["q"]["score"], results["q"]["reason"]) == (False, 0.0, reason)
    assert run["failed"] == 1


async def test_webhook_runner_timeout_fails_the_case(client, auth_headers, agent, hook):
    async def slow():
        await asyncio.sleep(5)
        return httpx.Response(200, json={"output": "late"})

    hook[1]["q"] = slow
    t0 = asyncio.get_running_loop().time()
    _, results = await _run(client, auth_headers, agent, [{"input": "q"}],
                            runner={"type": "webhook", "url": "https://runner.example.com/", "timeout_s": 0.2})
    assert results["q"]["reason"] == "runner_error: timeout" and not results["q"]["passed"]
    assert asyncio.get_running_loop().time() - t0 < 3


async def test_webhook_runner_refuses_private_addresses(client, auth_headers, agent, monkeypatch):
    sent = []
    monkeypatch.setattr(outbound, "_transport", httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200)))
    _, results = await _run(client, auth_headers, agent, [{"input": "q"}],
                            runner={"type": "webhook", "url": "http://127.0.0.1:9/case"})
    assert results["q"]["reason"] == "runner_error: blocked_address" and sent == []


async def test_traces_runner_takes_the_newest_matching_trace_of_the_agent(client, db, auth_headers, agent, org):
    other = Agent(org_id=org.id, name="other")
    await _add(db, other)
    now = datetime.utcnow()

    def t(input, output, ago_h, a=agent):
        return Trace(org_id=org.id, agent_id=a.id, trace_uid=f"{input}-{ago_h}", input=input, output=output,
                     created_at=now - timedelta(hours=ago_h))

    await _add(db, t(" q1 ", "new", 1), t("q1", "old", 5), t("q1", "other agent", 0, other),
               t("q2", "too old", 24 * 8), t("say q3 please", "contained", 2), t("Q4", "wrong case", 1),
               t("q5", "", 1), t("100% sure", "pct", 1), t("q6\n", "newline", 1))
    cases = [{"input": x, "expected_output": "new"} for x in ("q1", "q2", "q3", "q4", "q5", "%", "q6")]
    _, exact = await _run(client, auth_headers, agent, cases, runner={"type": "traces"})
    assert {k: (v["actual_output"], v["reason"]) for k, v in exact.items() if not v["passed"]} == {
        "q2": ("", "no_matching_trace"), "q3": ("", "no_matching_trace"), "q4": ("", "no_matching_trace"),
        "q5": ("", "no_output"), "%": ("", "no_matching_trace"), "q6": ("newline", "exact=0.0")}
    assert exact["q1"]["passed"] and exact["q1"]["actual_output"] == "new"

    _, contains = await _run(client, auth_headers, agent, cases, runner={"type": "traces", "match": "contains"})
    assert {k: v["actual_output"] or v["reason"] for k, v in contains.items()} == {
        "q1": "new", "q2": "no_matching_trace", "q3": "contained", "q4": "no_matching_trace", "q5": "no_output",
        "%": "pct", "q6": "newline"}

    _, longer = await _run(client, auth_headers, agent, cases[1:2], runner={"type": "traces", "lookback_days": 9})
    assert longer["q2"]["actual_output"] == "too old"


async def test_failed_cases_count_against_the_weighted_score(client, auth_headers, agent, hook):
    hook[1]["bad"] = (500, "x")
    run, _ = await _run(client, auth_headers, agent, [{"input": "good", "expected_output": "echo good", "weight": 3},
                                                      {"input": "bad", "expected_output": "", "weight": 1}],
                        runner={"type": "webhook", "url": "https://runner.example.com/"})
    assert (run["score"], run["passed"], run["failed"]) == (0.75, 1, 1)


# --- schedule_cron is honoured and scheduled runs use the set's agent ---

@pytest.mark.parametrize("expr", ["* * * * *", "*/5 * * * *", "0 9 * * mon-fri", "15,45 0-6/2 1 1-12 0",
                                  " 30  8  *  *  sun "])
def test_cron_accepts(expr):
    assert cron.parse(expr)


@pytest.mark.parametrize("expr", ["", "* * * *", "* * * * * *", "60 * * * *", "* 24 * * *", "* * 0 * *",
                                  "* * 32 * *", "* * * 13 *", "* * * * 7", "mon * * * *", "* * * jan *", "x * * * *",
                                  "*/0 * * * *", "@hourly"])
def test_cron_rejects(expr):
    with pytest.raises(ValueError):
        cron.parse(expr)


def test_cron_matches():
    at = datetime(2026, 10, 19, 9, 30)  # a Monday
    assert cron.matches(cron.parse("30 9 * * mon"), at)
    assert cron.matches(cron.parse("30 9 * * 1"), at)
    assert not cron.matches(cron.parse("30 9 * * 0"), at)
    assert not cron.matches(cron.parse("31 9 * * *"), at)
    assert cron.matches(cron.parse("*/15 */3 * * *"), at)
    assert not cron.matches(cron.parse("* * * 11 *"), at)
    # day of month and day of week both restricted: either one (as in cron)
    assert cron.matches(cron.parse("30 9 1 * mon"), at)
    assert cron.matches(cron.parse("30 9 19 * sun"), at)
    assert not cron.matches(cron.parse("30 9 1 * sun"), at)
    # only one restricted: that one
    assert not cron.matches(cron.parse("30 9 1 * *"), at)
    assert not cron.matches(cron.parse("30 9 * * sun"), at)


def test_cron_due_between():
    hourly = cron.parse("0 * * * *")
    assert cron.due(hourly, datetime(2026, 1, 1, 9, 10), datetime(2026, 1, 1, 9, 59, 59)) is None
    assert cron.due(hourly, datetime(2026, 1, 1, 9, 10), datetime(2026, 1, 1, 10, 0, 30)) == datetime(2026, 1, 1, 10)
    assert cron.due(hourly, datetime(2026, 1, 1, 10, 0, 30), datetime(2026, 1, 1, 10, 59)) is None
    assert cron.due(hourly, datetime(2026, 1, 1, 10), datetime(2026, 1, 1, 10, 0, 50)) is None  # that minute's done
    # long gaps: the latest due time within the last day, not a catch-up of each missed one
    assert cron.due(hourly, datetime(2025, 1, 1), datetime(2026, 1, 1, 10, 30)) == datetime(2026, 1, 1, 10)
    monthly = cron.parse("0 0 1 * *")
    assert cron.due(monthly, datetime(2025, 1, 1), datetime(2026, 1, 10)) is None


async def test_schedule_cron_is_validated(client, auth_headers):
    r = await client.post("/api/evaluations/sets", json={"name": "s", "schedule_cron": "every 5 minutes"},
                          headers=auth_headers)
    assert r.status_code == 422
    r = await client.post("/api/evaluations/sets", json={"name": "s", "schedule_cron": "*/5 * * * *"},
                          headers=auth_headers)
    assert r.status_code == 200 and r.json()["schedule_cron"] == "*/5 * * * *"
    sid = r.json()["id"]
    r = await client.patch(f"/api/evaluations/sets/{sid}", json={"schedule_cron": "* * * * 9"}, headers=auth_headers)
    assert r.status_code == 422
    r = await client.patch(f"/api/evaluations/sets/{sid}", json={"schedule_cron": "0 * * * *"}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["schedule_cron"] == "0 * * * *"


@pytest.fixture
def sync_db(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path}/s.db")
    Base.metadata.create_all(eng)
    Session = sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(evaluation_tasks, "SyncSession", Session)
    yield Session
    eng.dispose()


@pytest.fixture
def delayed(monkeypatch, sync_db):
    """Runs handed to the worker, each with whether its row was committed by then."""
    out = []

    def delay(run_id):
        with sync_db() as s:
            out.append((run_id, s.get(EvaluationRun, run_id) is not None))

    monkeypatch.setattr(evaluation_tasks.execute_run, "delay", delay)
    return out


def _seed_schedule(Session, created, **sets):
    """An org with agents a (made first) and b; sets name -> (cron, agent of its last run or None)."""
    with Session() as s:
        o, other = Organization(name="o", slug="o"), Organization(name="p", slug="p")
        s.add_all([o, other])
        s.flush()
        a = Agent(org_id=o.id, name="a", current_version="1.0.0", created_at=created)
        s.add(a)
        s.flush()
        b = Agent(org_id=o.id, name="b", current_version="2.1.0", created_at=created + timedelta(seconds=1))
        s.add(b)
        agents = {"a": a, "b": b}
        ids = {}
        for name, (expr, last_agent) in sets.items():
            es = EvaluationSet(org_id=o.id, name=name, schedule_cron=expr, created_at=created)
            s.add(es)
            s.flush()
            if last_agent:
                s.add(EvaluationRun(set_id=es.id, agent_id=agents[last_agent].id, agent_version="0.9",
                                    triggered_by="someone", created_at=created + timedelta(minutes=1)))
            ids[name] = es.id
        s.commit()
        return ids, {k: v.id for k, v in agents.items()}


def test_scheduled_runs_follow_the_cron_and_the_sets_agent(sync_db, delayed):
    t0 = datetime(2026, 1, 1, 9, 10)
    sets, agents = _seed_schedule(sync_db, t0, hourly=("0 * * * *", "b"), never_run=("* * * * *", None),
                                  unscheduled=(None, "a"), five=("*/5 * * * *", "a"))
    assert evaluation_tasks.schedule_due_runs(t0 + timedelta(minutes=2)) == 0  # 09:12: nothing matches since 09:11
    assert evaluation_tasks.schedule_due_runs(t0 + timedelta(minutes=5, seconds=10)) == 1  # 09:15: five
    assert evaluation_tasks.schedule_due_runs(t0 + timedelta(minutes=5, seconds=40)) == 0
    assert evaluation_tasks.schedule_due_runs(t0 + timedelta(minutes=50, seconds=5)) == 2  # 10:00: hourly, five
    assert evaluation_tasks.schedule_due_runs(t0 + timedelta(minutes=51)) == 0
    with sync_db() as s:
        runs = s.execute(select(EvaluationRun).where(EvaluationRun.triggered_by == "scheduled")
                         .order_by(EvaluationRun.created_at, EvaluationRun.set_id)).scalars().all()
        got = sorted((r.set_id, r.agent_id, r.agent_version, r.created_at) for r in runs)
    assert got == sorted([(sets["five"], agents["a"], "1.0.0", t0 + timedelta(minutes=5, seconds=10)),
                          (sets["five"], agents["a"], "1.0.0", t0 + timedelta(minutes=50, seconds=5)),
                          (sets["hourly"], agents["b"], "2.1.0", t0 + timedelta(minutes=50, seconds=5))])
    assert sorted(delayed) == sorted((r.id, True) for r in runs)


def test_scheduled_set_follows_its_latest_run(sync_db, delayed):
    t0 = datetime(2026, 1, 1, 9, 10)
    sets, agents = _seed_schedule(sync_db, t0, s=("* * * * *", "a"))
    with sync_db() as s:
        s.add(EvaluationRun(set_id=sets["s"], agent_id=agents["b"], triggered_by="u", created_at=t0 + timedelta(minutes=2)))
        s.commit()
    assert evaluation_tasks.schedule_due_runs(t0 + timedelta(minutes=3)) == 1
    with sync_db() as s:
        run = s.execute(select(EvaluationRun).where(EvaluationRun.triggered_by == "scheduled")).scalar_one()
    assert run.agent_id == agents["b"]


def test_a_bad_stored_cron_skips_only_its_set(sync_db, delayed, caplog):
    t0 = datetime(2026, 1, 1, 9, 10)
    sets, _ = _seed_schedule(sync_db, t0, bad=("whenever", "a"), good=("* * * * *", "a"))
    assert evaluation_tasks.schedule_due_runs(t0 + timedelta(minutes=3)) == 1
    assert "whenever" in caplog.text


def test_scheduled_evaluations_task_and_beat(sync_db, delayed):
    _seed_schedule(sync_db, datetime.utcnow() - timedelta(hours=2), s=("* * * * *", "a"))
    assert evaluation_tasks.run_scheduled_evaluations.run() == {"spawned": 1}
    beat = {v["task"]: v["schedule"] for v in celery.conf.beat_schedule.values()}
    assert beat[evaluation_tasks.run_scheduled_evaluations.name] == 60.0  # minute-level crons need a minute tick


# --- alerts: one cost_per_day, and periodic alerts open incidents like violation alerts ---

def _trace(org, agent, ago, **kw):
    return Trace(org_id=org.id, agent_id=agent.id, trace_uid=f"t{ago}{kw}", created_at=datetime.utcnow() - ago, **kw)


async def test_cost_per_day_sums_the_last_day_whatever_the_window(db, org, agent):
    other = Agent(org_id=org.id, name="other")
    await _add(db, other)
    await _add(db, _trace(org, agent, timedelta(minutes=5), cost_usd=0.5), _trace(org, agent, timedelta(hours=2), cost_usd=1.0),
               _trace(org, agent, timedelta(hours=20), cost_usd=1.0), _trace(org, agent, timedelta(hours=30), cost_usd=4.0),
               _trace(org, other, timedelta(hours=1), cost_usd=8.0))
    rule = lambda **kw: AlertRule(org_id=org.id, metric="cost_per_day", operator=">", threshold=0, **kw)  # noqa: E731
    assert await compute_metric_value(db, rule(window_minutes=15, agent_id=agent.id)) == 2.5
    assert await compute_metric_value(db, rule(window_minutes=60 * 24 * 7, agent_id=agent.id)) == 2.5
    assert await compute_metric_value(db, rule(window_minutes=1)) == 10.5
    assert await compute_metric_value(db, rule(window_minutes=1, agent_id="nobody")) == 0.0


def test_alert_task_has_no_metric_code_of_its_own():
    assert not hasattr(alert_tasks, "_compute") and not hasattr(alert_tasks, "_compare")


@pytest.fixture
def dispatched(monkeypatch):
    out = []

    async def dispatch(channels, message, severity):
        out.append((channels, message, severity))
        return [True] * len(channels)

    monkeypatch.setattr(alert_service, "dispatch_alert", dispatch)
    return out


async def test_periodic_alerts_fire_open_incidents_and_dedup(db, org, agent, dispatched):
    await _add(db, *(_trace(org, agent, timedelta(minutes=i), status="error" if i < 3 else "success")
                     for i in range(1, 5)))
    chan = [{"type": "slack", "target": "https://hooks.example.com/x"}]
    crit, warn, quiet, off, pv = await _add(
        db,
        AlertRule(org_id=org.id, agent_id=agent.id, name="crit", metric="error_rate", operator=">", threshold=0.4,
                  severity="critical", channels=chan),
        AlertRule(org_id=org.id, name="warn", metric="error_rate", operator=">", threshold=0.4, severity="warning"),
        AlertRule(org_id=org.id, name="quiet", metric="error_rate", operator=">", threshold=0.9, severity="critical"),
        AlertRule(org_id=org.id, name="off", metric="error_rate", operator=">", threshold=0, severity="critical",
                  enabled=False),
        AlertRule(org_id=org.id, name="pv", metric="policy_violations", operator=">=", threshold=0, severity="critical"))
    assert await evaluate_metric_rules(db) == {"checked": 3, "fired": 2}
    events = await _all(db, AlertEvent)
    assert sorted((e.rule_id, e.value) for e in events) == sorted([(crit.id, 0.5), (warn.id, 0.5)])
    (inc,) = await _all(db, Incident)
    assert (inc.org_id, inc.agent_id, inc.severity, inc.status, inc.title) == \
        (org.id, agent.id, "critical", "open", "crit: error_rate breached")
    assert inc.timeline[0]["event"] == "alert_fired"
    assert dispatched == [(chan, f"crit: {next(e for e in events if e.rule_id == crit.id).message}", "critical")]

    assert await evaluate_metric_rules(db) == {"checked": 3, "fired": 0}  # still firing within dedup
    assert len(await _all(db, AlertEvent)) == 2 and len(await _all(db, Incident)) == 1 and len(dispatched) == 1


async def test_periodic_alert_task_runs_the_shared_evaluation(session_maker, db, org, agent, dispatched, monkeypatch):
    await _add(db, _trace(org, agent, timedelta(hours=3), cost_usd=3.0),
               AlertRule(org_id=org.id, name="spend", metric="cost_per_day", operator=">=", threshold=3,
                         window_minutes=15, severity="critical"))
    monkeypatch.setattr(alert_tasks, "async_sessionmaker", lambda *a, **k: session_maker)
    assert await asyncio.to_thread(alert_tasks.evaluate_alert_rules.run) == {"checked": 1, "fired": 1}
    (event,) = await _all(db, AlertEvent)
    assert event.value == 3.0 and len(await _all(db, Incident)) == 1


# --- retention_days is enforced by a beat task ---

async def test_retention_deletes_traces_past_each_orgs_window(db, session_maker, monkeypatch):
    short, long_, keep = await _add(db, Organization(name="s", slug="s", retention_days=30),
                                    Organization(name="l", slug="l", retention_days=90),
                                    Organization(name="k", slug="k", retention_days=0))
    agents = await _add(db, *(Agent(org_id=o.id, name=o.slug) for o in (short, long_, keep)))
    traces = {}
    for o, a in zip((short, long_, keep), agents):
        for days in (10, 40, 100):
            traces[(o.slug, days)] = Trace(org_id=o.id, agent_id=a.id, trace_uid=f"{o.slug}{days}",
                                           created_at=datetime.utcnow() - timedelta(days=days))
    await _add(db, *traces.values())
    old = traces[("s", 40)]
    pol = (await _add(db, TracePolicy(org_id=short.id, name="p", source="x", severity="high")))[0]
    await _add(db, Span(trace_id=old.id, seq=0, name="tool"),
               TracePolicyViolation(org_id=short.id, trace_id=old.id, agent_id=old.agent_id, policy_id=pol.id,
                                    policy_name="p", policy_version=1, severity="high", at_seq=0, mode="batch"))
    assert await retention_tasks.purge_expired_traces(db) == 3
    left = {t.trace_uid for t in await _all(db, Trace)}
    assert left == {"s10", "l10", "l40", "k10", "k40", "k100"}
    assert await _all(db, Span) == [] and await _all(db, TracePolicyViolation) == []
    assert await retention_tasks.purge_expired_traces(db) == 0

    monkeypatch.setattr(retention_tasks, "async_sessionmaker", lambda *a, **k: session_maker)
    await _add(db, Trace(org_id=short.id, agent_id=agents[0].id, trace_uid="x",
                         created_at=datetime.utcnow() - timedelta(days=31)))
    assert await asyncio.to_thread(retention_tasks.enforce_retention.run) == 1
    beat = {v["task"] for v in celery.conf.beat_schedule.values()}
    assert retention_tasks.enforce_retention.name in beat
    assert "app.tasks.retention_tasks" in celery.conf.include


# --- integrations that only store config say so ---

CONFIG_ONLY = {"azure": "Azure OpenAI", "msteams": "Microsoft Teams", "grafana": "Grafana", "newrelic": "New Relic",
               "github_actions": "GitHub Actions", "gitlab_ci": "GitLab CI"}
PRESENCE_ONLY = {"openai": ("api_key", "key_present"), "anthropic": ("api_key", "key_present"),
                 "cohere": ("api_key", "key_present"), "pagerduty": ("routing_key", "routing_key_present")}


async def test_config_only_integrations_call_nothing(client, auth_headers, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("no request expected")

    monkeypatch.setattr(outbound, "post", refuse)
    monkeypatch.setattr(httpx.AsyncClient, "get", refuse)  # the test client only posts
    for kind, (field, message) in PRESENCE_ONLY.items():
        r = await client.post("/api/integrations", json={"kind": kind, "name": kind, "config": {field: "k"}},
                              headers=auth_headers)
        r = await client.post(f"/api/integrations/{r.json()['id']}/test", headers=auth_headers)
        assert r.json() == {"ok": True, "message": message, "details": {}}, kind
    for kind in CONFIG_ONLY:
        r = await client.post("/api/integrations", json={"kind": kind, "name": kind, "config": {"api_key": "k"}},
                              headers=auth_headers)
        r = await client.post(f"/api/integrations/{r.json()['id']}/test", headers=auth_headers)
        assert r.json() == {"ok": True, "message": "no specific check; configured", "details": {}}, kind


def _readme_line(label: str) -> str:
    (line,) = [x for x in README.splitlines() if x.startswith(f"- **{label}**")]
    return line


def test_readme_labels_config_only_integrations():
    line = _readme_line("Integrations")
    config_only = re.search(r"store their config only[^.]*:([^.]*)\.", line)
    assert config_only, line
    assert {x.strip() for x in re.split(r",| and ", config_only.group(1)) if x.strip()} == set(CONFIG_ONLY.values())
    presence = re.search(r"checked for presence only:([^.]*)\.", line)
    assert presence and all(name in presence.group(1) for name in ("OpenAI", "Anthropic", "Cohere", "PagerDuty"))
    assert "key validation" not in line


def test_readme_feature_claims_match_the_code():
    evals = _readme_line("Evaluations")
    assert "runner" in evals and "no output fails" in evals and "schedule_cron" in evals
    alerts = _readme_line("Alerting & incidents")
    assert "cost_per_day (last 24 h)" in alerts and "incidents for critical rules and critical policy violations" in alerts
    assert "retention_days" in _readme_line("Telemetry ingestion")
    assert "/tmp/agentops_sdk/" not in README and "import agentops\n" not in README
    assert "import agentops_sdk as agentops" in README


async def test_seeded_eval_sets_have_runners_and_the_scheduled_one_an_agent(session_maker, db, monkeypatch):
    from types import SimpleNamespace

    from app import seed

    async def _noop():
        pass
    monkeypatch.setattr(seed, "create_async_engine", lambda *a, **k: SimpleNamespace(dispose=_noop))
    monkeypatch.setattr(seed, "async_sessionmaker", lambda *a, **k: session_maker)
    await seed.seed()
    sets = await _all(db, EvaluationSet)
    assert sets and all(s.runner for s in sets)
    for s in sets:
        if s.schedule_cron:
            cron.parse(s.schedule_cron)
            assert await _all(db, EvaluationRun, EvaluationRun.set_id == s.id)


def test_a_manual_run_does_not_stand_in_for_a_scheduled_one(sync_db, delayed):
    t0 = datetime(2026, 1, 1, 9, 10)
    sets, agents = _seed_schedule(sync_db, t0, s=("0 * * * *", "a"))
    with sync_db() as s:
        s.add(EvaluationRun(set_id=sets["s"], agent_id=agents["a"], triggered_by="u",
                            created_at=datetime(2026, 1, 1, 10, 0, 5)))
        s.commit()
    assert evaluation_tasks.schedule_due_runs(datetime(2026, 1, 1, 10, 0, 20)) == 1
