"""SG-AP1.4: the prod promotion gate reads the promoted version's runs; a run's version is one of the agent's."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import seed
from app.config import settings
from app.middleware.auth import make_access_token
from app.models import Agent, AgentVersion, EvaluationRun, EvaluationSet, Organization, OrgMembership, Trace, User


async def _add(db, *rows):
    db.add_all(rows)
    await db.commit()
    return rows


@pytest.fixture
async def versions(db, agent):
    await _add(db, *(AgentVersion(agent_id=agent.id, version=v, model_name="m") for v in ("1.0.0", "2.0.0")))
    s = (await _add(db, EvaluationSet(org_id=agent.org_id, name="s")))[0]
    t0 = datetime.utcnow() - timedelta(hours=1)

    async def run(version, score, minutes, status="completed"):
        await _add(db, EvaluationRun(set_id=s.id, agent_id=agent.id, agent_version=version, status=status,
                                     score=score, created_at=t0 + timedelta(minutes=minutes)))
    return run


async def _promote(client, headers, agent, version, env="prod", **kw):
    return await client.post(f"/api/agents/{agent.id}/promote", headers=headers,
                             json={"target_environment": env, "version": version, **kw})


async def test_another_versions_passing_run_does_not_open_the_gate(client, auth_headers, agent, versions):
    await versions("1.0.0", 1.0, 0)
    r = await _promote(client, auth_headers, agent, "2.0.0")
    assert r.status_code == 400 and "eval_gate_blocked: last_score=None" in r.text
    assert (await _promote(client, auth_headers, agent, "1.0.0")).status_code == 200


async def test_the_versions_latest_completed_run_decides(client, auth_headers, agent, versions):
    await versions("2.0.0", 0.9, 0)
    await versions("2.0.0", 0.2, 1)
    await versions("1.0.0", 1.0, 2)
    await versions("2.0.0", 1.0, 3, status="running")
    await versions("2.0.0", 1.0, 4, status="failed")
    r = await _promote(client, auth_headers, agent, "2.0.0")
    assert r.status_code == 400 and "last_score=0.2" in r.text
    await versions("2.0.0", 0.8, 5)
    assert (await _promote(client, auth_headers, agent, "2.0.0")).status_code == 200


async def test_force_and_non_prod_targets_skip_the_gate(client, auth_headers, agent, versions):
    r = await _promote(client, auth_headers, agent, "2.0.0")
    assert r.status_code == 400 and "eval_gate_blocked" in r.text
    r = await _promote(client, auth_headers, agent, "2.0.0", force=True)
    assert r.status_code == 200 and r.json()["environment"] == "prod"
    assert (await _promote(client, auth_headers, agent, "1.0.0", env="staging")).status_code == 200


async def _run(client, headers, agent, cases, version=None):
    body = {"name": "s", "evaluators": ["exact"], "cases": cases, "runner": {"type": "traces"}}
    sid = (await client.post("/api/evaluations/sets", headers=headers, json=body)).json()["id"]
    r = await client.post(f"/api/evaluations/sets/{sid}/run", headers=headers,
                          json={"agent_id": agent.id, "agent_version": version})
    assert r.status_code == 200, r.text
    detail = (await client.get(f"/api/evaluations/runs/{r.json()['id']}", headers=headers)).json()
    return r.json(), [(x["actual_output"], x["reason"]) for x in detail["results"]]


async def test_traces_runner_scores_only_traces_of_the_runs_version(client, db, org, auth_headers, agent, versions):
    now = datetime.utcnow()
    await _add(db, *(Trace(org_id=org.id, agent_id=agent.id, trace_uid=f"{i}", input=q, output=out, agent_version=v,
                           created_at=now - timedelta(minutes=m))
                     for i, (q, out, v, m) in enumerate([("q", "v1", "1.0.0", 1), ("q", "v2", "2.0.0", 5),
                                                         ("only1", "x", "1.0.0", 1)])))
    run, got = await _run(client, auth_headers, agent, [{"input": "q", "expected_output": "v2"}], "2.0.0")
    assert (run["agent_version"], run["score"], got) == ("2.0.0", 1.0, [("v2", "exact=1.0")])
    run, got = await _run(client, auth_headers, agent, [{"input": "q", "expected_output": "v2"}])
    assert (run["agent_version"], got) == ("1.0.0", [("v1", "exact=0.0")])
    _, got = await _run(client, auth_headers, agent, [{"input": "only1"}], "2.0.0")
    assert got == [("", "no_matching_trace")]


async def test_a_run_of_a_version_the_agent_lacks_is_404(client, db, org, auth_headers, agent, versions):
    other = (await _add(db, Agent(org_id=org.id, name="other")))[0]
    await _add(db, AgentVersion(agent_id=other.id, version="3.0.0", model_name="m"))
    sid = (await client.post("/api/evaluations/sets", headers=auth_headers, json={"name": "s"})).json()["id"]
    for v in ("9.9.9", "3.0.0", "1.0"):
        r = await client.post(f"/api/evaluations/sets/{sid}/run", headers=auth_headers,
                              json={"agent_id": agent.id, "agent_version": v})
        assert (r.status_code, r.json()["error"]["message"]) == (404, "version_not_found")
    assert (await db.execute(select(EvaluationRun).where(EvaluationRun.set_id == sid))).first() is None
    r = await client.post(f"/api/evaluations/sets/{sid}/run", headers=auth_headers,
                          json={"agent_id": agent.id, "agent_version": "2.0.0"})
    assert r.status_code == 200 and r.json()["agent_version"] == "2.0.0"


async def test_the_agents_current_version_runs_without_a_version_row(client, db, auth_headers, agent):
    sid = (await client.post("/api/evaluations/sets", headers=auth_headers, json={"name": "s"})).json()["id"]
    for v in (None, agent.current_version):
        r = await client.post(f"/api/evaluations/sets/{sid}/run", headers=auth_headers,
                              json={"agent_id": agent.id, "agent_version": v})
        assert r.status_code == 200 and r.json()["agent_version"] == "1.0.0"


@pytest.mark.parametrize("weight", [0, -0.5, -1])
async def test_case_weights_must_be_positive(client, auth_headers, weight):
    r = await client.post("/api/evaluations/sets", headers=auth_headers,
                          json={"name": "s", "cases": [{"input": "q", "weight": weight}]})
    assert r.status_code == 422
    sid = (await client.post("/api/evaluations/sets", headers=auth_headers, json={"name": "s"})).json()["id"]
    r = await client.post(f"/api/evaluations/sets/{sid}/cases", headers=auth_headers,
                          json={"input": "q", "weight": weight})
    assert r.status_code == 422
    r = await client.post(f"/api/evaluations/sets/{sid}/cases", headers=auth_headers, json={"input": "q", "weight": 0.5})
    assert r.status_code == 200 and r.json()["weight"] == 0.5


async def test_the_seeded_demo_promotes_only_its_passing_agent(client, db, session_maker, monkeypatch):
    async def _noop():
        pass
    monkeypatch.setattr(seed, "create_async_engine", lambda *a, **k: SimpleNamespace(dispose=_noop))
    monkeypatch.setattr(seed, "async_sessionmaker", lambda *a, **k: session_maker)
    await seed.seed()
    org = (await db.execute(select(Organization).where(Organization.slug == "demo"))).scalar_one()
    owner = (await db.execute(select(User).join(OrgMembership).where(
        User.email == settings.DEMO_EMAIL, OrgMembership.org_id == org.id))).scalar_one()
    headers = {"Authorization": f"Bearer {make_access_token(owner.id, org.id, 'owner')}"}
    agents = {a.name: a for a in (await db.execute(select(Agent).where(Agent.org_id == org.id))).scalars()}
    r = await _promote(client, headers, agents["Refund Bot"], "1.0.0")
    assert r.status_code == 200, r.text
    for name in ("Support Triage", "Doc Summarizer"):  # a failed run; no run
        r = await _promote(client, headers, agents[name], "1.0.0")
        assert r.status_code == 400 and "eval_gate_blocked" in r.text
