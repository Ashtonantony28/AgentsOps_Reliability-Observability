"""AUDIT-16 (platform after SG-AP1.1 to SG-AP1.4: alert tick, login and password hashing, outbound, promotion gate).

Each test is xfail(strict=True): it fails today at its final assertion, after its control assertions pass, and will
XPASS (failing CI) once its finding is fixed, so the fixing sub-goal removes the marker."""
import asyncio
import socket
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.models import AlertEvent, AlertRule, Organization, Trace
from app.services import auth_service, outbound
from app.services.alert_service import evaluate_metric_rules

PUBLIC_IP = "93.184.216.34"


@pytest.fixture
def net(monkeypatch):
    """Outbound requests resolve to a public address and go to a mock transport; `seen` lists the hosts called."""
    seen = []

    async def resolve(host, port):
        return [PUBLIC_IP]

    def handler(request):
        seen.append(request.headers["host"])
        return httpx.Response(200, json={"output": "ok"})

    monkeypatch.setattr(outbound, "_resolve", resolve)
    monkeypatch.setattr(outbound, "_transport", httpx.MockTransport(handler))
    return seen


# --- A16-1 (high): one rule that raises in the check phase drops every org's pages ---

@pytest.mark.xfail(strict=True, reason="A16-1: a rule whose check raises (window_minutes past datetime's range) ends "
                                       "the tick before any page is sent, and dedup then suppresses the fired alerts")
async def test_one_orgs_failing_rule_does_not_drop_another_orgs_page(client, db, session_maker, org, auth_headers,
                                                                     net):
    other = Organization(name="B", slug=f"b-{uuid.uuid4().hex[:8]}")
    db.add(other)
    await db.commit()
    db.add(AlertRule(org_id=other.id, name="b", metric="token_usage", operator=">=", threshold=0,
                     created_at=datetime.utcnow() - timedelta(hours=1),
                     channels=[{"type": "webhook", "target": "https://b.example/hook", "name": ""}]))
    await db.commit()
    rule = {"name": "a", "metric": "token_usage", "operator": ">=", "threshold": 0, "window_minutes": 2_000_000_000}
    r = await client.post("/api/alerts/rules", headers=auth_headers, json=rule)
    assert r.status_code in (200, 422)
    if r.status_code == 422:  # a row stored before the API checked
        db.add(AlertRule(org_id=org.id, **rule))
        await db.commit()
    async with session_maker() as s:
        try:
            await evaluate_metric_rules(s)
        except Exception:
            pass
    async with session_maker() as s:
        fired = (await s.execute(select(func.count()).select_from(AlertEvent).where(
            AlertEvent.org_id == other.id))).scalar_one()
    assert fired == 1  # B's rule fired and was recorded, so dedup holds it for 30 minutes
    assert net == ["b.example"], "B's alert fired but was never paged"


# --- A16-2 (high): outbound lookups hold the default executor past their deadline ---

@pytest.mark.xfail(strict=True, reason="A16-2: outbound resolves names with loop.getaddrinfo on the default executor; "
                                       "the deadline cancels the wait, not the thread, so ingest parsing waits behind it")
async def test_a_slow_outbound_lookup_does_not_delay_another_orgs_ingest(client, auth_headers, agent, api_key_headers,
                                                                         monkeypatch):
    real = socket.getaddrinfo

    def getaddrinfo(host, *a, **kw):
        if host == "slow-dns.example":  # a name server that does not answer
            time.sleep(1.0)
            raise socket.gaierror("timeout")
        return real(host, *a, **kw)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(outbound, "RESOLVE_TIMEOUT_S", 0.2)
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(2))

    async def ingest():
        t = time.perf_counter()
        r = await client.post("/api/traces/ingest", headers=api_key_headers,
                              json={"agent_id": agent.id, "input": "q", "output": "a"})
        assert r.status_code == 202, r.text
        return time.perf_counter() - t

    assert await ingest() < 0.5  # alone
    r = await client.post("/api/integrations", headers=auth_headers,
                          json={"kind": "webhook", "name": "w", "config": {"url": "https://slow-dns.example/"}})
    iid = r.json()["id"]
    t = time.perf_counter()
    tests = await asyncio.gather(*(client.post(f"/api/integrations/{iid}/test", headers=auth_headers)
                                   for _ in range(2)))
    assert all(r.status_code == 200 and r.json()["ok"] is False for r in tests)
    assert time.perf_counter() - t < 0.6  # answered at the lookup's deadline
    took = await ingest()
    assert took < 0.5, f"ingest took {took:.2f} s behind lookups that had already timed out"


# --- A16-3 (high): password changes fill the hash queue, and every login is refused ---

@pytest.mark.xfail(strict=True, reason="A16-3: /api/auth/password is not limited and shares the 16-place hash queue, so "
                                       "one signed-in user's wrong-password changes make every login 429")
async def test_password_changes_do_not_refuse_another_users_login(client, db, org, auth_headers, viewer_user,
                                                                  monkeypatch):
    def slow_verify(pw, hashed):  # stands in for bcrypt
        time.sleep(0.2)
        return False
    monkeypatch.setattr(auth_service, "verify_password", slow_verify)
    login = {"email": viewer_user.email, "password": "wrong-password"}
    assert (await client.post("/api/auth/login", json=login)).status_code == 401  # alone
    change = {"current_password": "wrong-password", "new_password": "new-password-1"}
    changes = [asyncio.create_task(client.post("/api/auth/password", headers=auth_headers, json=change))
               for _ in range(16)]
    await asyncio.sleep(0.05)
    r = await client.post("/api/auth/login", json=login)
    await asyncio.gather(*changes)
    assert r.status_code == 401, f"another user's login answered {r.status_code}"


# --- A16-4 (medium): the latency and cost evaluators read every version's traces ---

@pytest.mark.xfail(strict=True, reason="A16-4: run_evaluation's latency and cost evaluators take the agent's last 100 "
                                       "traces of any version, so a slow version scores on an older one's latency")
async def test_latency_evaluator_reads_the_runs_version(client, db, org, auth_headers, agent):
    agent.current_version = "2.0.0"
    await db.commit()
    now = datetime.utcnow()
    db.add_all([Trace(org_id=org.id, agent_id=agent.id, trace_uid=f"n{i}", input="q", output="ok",
                      agent_version="2.0.0", latency_ms=10_000, created_at=now - timedelta(minutes=10))
                for i in range(5)])
    db.add_all([Trace(org_id=org.id, agent_id=agent.id, trace_uid=f"o{i}", input="other", output="x",
                      agent_version="1.0.0", latency_ms=100, created_at=now - timedelta(seconds=i))
                for i in range(100)])
    await db.commit()
    body = {"name": "s", "evaluators": ["latency"], "cases": [{"input": "q", "expected_output": "ok"}],
            "runner": {"type": "traces"}}
    sid = (await client.post("/api/evaluations/sets", headers=auth_headers, json=body)).json()["id"]
    run = (await client.post(f"/api/evaluations/sets/{sid}/run", headers=auth_headers,
                             json={"agent_id": agent.id})).json()
    assert (run["agent_version"], run["status"], run["total"]) == ("2.0.0", "completed", 1)
    assert run["score"] < 0.7, "2.0.0's p95 is 10 s, scored on 1.0.0's 100 ms"


# --- A16-5 (medium): one address's wrong logins lock an email out for everyone ---

@pytest.mark.xfail(strict=True, reason="A16-5: the per-email login bound counts every address's attempts, so anyone "
                                       "can keep any account's sign-in refused, whatever the password")
async def test_wrong_logins_from_one_address_do_not_lock_the_owner_out(app_, db, viewer_user, monkeypatch):
    monkeypatch.setattr(auth_service, "verify_password", lambda pw, hashed: pw == "right-password")

    def at(ip):
        return AsyncClient(transport=ASGITransport(app=app_, client=(ip, 1234)), base_url="http://test")
    async with at("198.51.100.7") as other, at("203.0.113.9") as owner:
        for _ in range(10):
            r = await other.post("/api/auth/login", json={"email": viewer_user.email, "password": "guess"})
            assert r.status_code == 401
        r = await owner.post("/api/auth/login", json={"email": viewer_user.email, "password": "right-password"})
    assert r.status_code == 200, f"the owner's right password from their own address: {r.status_code}"
