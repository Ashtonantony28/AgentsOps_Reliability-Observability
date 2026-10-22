"""AUDIT-15 (platform: auth and tenancy, outbound requests, webhooks and Slack OAuth, evals, alerts, retention).

Each test is xfail(strict=True): it fails today at its final assertion, after its control assertions pass, and will
XPASS (failing CI) once its finding is fixed, so the fixing sub-goal removes the marker."""
import asyncio
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import func, select
from starlette.websockets import WebSocketDisconnect

from app.celery_app import celery
from app.config import settings
from app.middleware.auth import decode_token
from app.models import AlertEvent, AlertRule, Organization
from app.routers import integrations as integrations_router
from app.routers import ws as ws_router
from app.services import auth_service, outbound
from app.services.alert_service import evaluate_metric_rules

PUBLIC_IP = "93.184.216.34"
MiB = 1 << 20


@pytest.fixture
def net(monkeypatch):
    """Outbound requests resolve to a public address and go to `handlers[host]` (default: 200 {"output": "ok"})."""
    handlers, seen = {}, []

    async def resolve(host, port):
        return [PUBLIC_IP]

    async def handler(request):
        host = request.headers["host"]
        seen.append(host)
        h = handlers.get(host)
        return await h(request) if h else httpx.Response(200, json={"output": "ok"})

    monkeypatch.setattr(outbound, "_resolve", resolve)
    monkeypatch.setattr(outbound, "_transport", httpx.MockTransport(handler))
    return handlers, seen


# --- A15-1 (high): one org's alert channels hold up every later org's periodic alerts ---

@pytest.mark.xfail(strict=True, reason="A15-1: periodic alert rules of every org are checked in one serial loop that "
                                       "awaits each firing rule's channels before the next org's rule is checked")
async def test_a_slow_alert_channel_of_one_org_does_not_hold_up_another_orgs_alert(db, session_maker, org, net):
    handlers, seen = net
    other = Organization(name="B", slug=f"b-{uuid.uuid4().hex[:8]}")
    db.add(other)
    await db.commit()
    t0 = datetime.utcnow() - timedelta(hours=1)
    # org A's rule (older) pages a webhook that answers slowly; org B's rule has no channel at all
    db.add_all([
        AlertRule(org_id=org.id, name="a", metric="token_usage", operator=">=", threshold=0, created_at=t0,
                  channels=[{"type": "webhook", "target": "https://slow.example/hook", "name": ""}]),
        AlertRule(org_id=other.id, name="b", metric="token_usage", operator=">=", threshold=0,
                  created_at=t0 + timedelta(minutes=1), channels=[]),
    ])
    await db.commit()
    release = asyncio.Event()

    async def slow(request):
        await release.wait()
        return httpx.Response(200)
    handlers["slow.example"] = slow

    async def fired(org_id):
        async with session_maker() as s:
            return (await s.execute(select(func.count()).select_from(AlertEvent).where(
                AlertEvent.org_id == org_id))).scalar_one()

    async with session_maker() as s:
        task = asyncio.create_task(evaluate_metric_rules(s))
        for _ in range(200):
            if seen:
                break
            await asyncio.sleep(0.01)
        assert seen == ["slow.example"] and await fired(org.id) == 1  # org A fired and is paging
        await asyncio.sleep(0.3)
        b_while_a_pages = await fired(other.id)
        release.set()
        summary = await asyncio.wait_for(task, 5)
    assert summary == {"checked": 2, "fired": 2} and await fired(other.id) == 1  # B fires once A's page returns
    assert b_while_a_pages == 1  # ... but not while it is being sent


# --- A15-2 (high): unlimited logins hash passwords on the executor that ingest parsing shares ---

@pytest.mark.xfail(strict=True, reason="A15-2: /api/auth/login has no rate limit and its bcrypt runs on the default "
                                       "executor, so wrong-password logins delay every org's ingest parsing")
async def test_failed_logins_do_not_delay_another_orgs_ingest(client, db, org, admin_user, agent, api_key_headers,
                                                              monkeypatch):
    def slow_verify(pw, hashed):  # stands in for bcrypt (0.3 s at the default cost)
        time.sleep(0.5)
        return False
    monkeypatch.setattr(auth_service, "verify_password", slow_verify)
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(2))  # min(32, cpus + 4) in production

    async def ingest():
        t = time.perf_counter()
        r = await client.post("/api/traces/ingest", headers=api_key_headers,
                              json={"agent_id": agent.id, "input": "q", "output": "a"})
        assert r.status_code == 202, r.text
        return time.perf_counter() - t

    assert await ingest() < 0.5  # alone
    login = {"email": admin_user.email, "password": "wrong-password"}
    logins = [asyncio.create_task(client.post("/api/auth/login", json=login)) for _ in range(8)]
    await asyncio.sleep(0.05)
    took = await ingest()
    assert {r.status_code for r in await asyncio.gather(*logins)} <= {401, 429}  # wrong passwords
    assert took < 0.5, f"ingest took {took:.2f} s behind 8 failed logins"


# --- A15-3 (high): outbound responses are read whole ---

@pytest.mark.xfail(strict=True, reason="A15-3: outbound.post reads the whole response body into memory, so a "
                                       "configured URL can make the API process buffer any amount")
async def test_connection_test_does_not_buffer_an_unbounded_response(client, db, org, auth_headers, net):
    handlers, _ = net
    sent = [0]

    class Huge(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(64):
                sent[0] += MiB
                yield b"x" * MiB

    async def huge(request):
        return httpx.Response(200, stream=Huge())
    handlers["huge.example"] = huge
    r = await client.post("/api/integrations", headers=auth_headers,
                          json={"kind": "webhook", "name": "w", "config": {"url": "https://small.example/"}})
    small = r.json()["id"]
    r = await client.post(f"/api/integrations/{small}/test", headers=auth_headers)
    assert r.status_code == 200 and r.json()["ok"] is True  # a normal reply
    r = await client.post("/api/integrations", headers=auth_headers,
                          json={"kind": "webhook", "name": "w", "config": {"url": "https://huge.example/"}})
    r = await client.post(f"/api/integrations/{r.json()['id']}/test", headers=auth_headers)
    assert r.status_code == 200
    assert sent[0] <= 2 * MiB, f"read {sent[0] // MiB} MiB of the reply"


# --- A15-4 (high): the prod promotion gate does not look at the version being promoted ---

async def test_promotion_gate_needs_a_passing_run_of_the_promoted_version(client, db, org, auth_headers, agent, net):
    promote = {"target_environment": "prod", "version": "1.0.0"}
    r = await client.post(f"/api/agents/{agent.id}/promote", headers=auth_headers, json=promote)
    assert r.status_code == 400 and "eval_gate_blocked" in r.text  # no run: blocked
    body = {"name": "s", "evaluators": ["exact"], "cases": [{"input": "q", "expected_output": "ok"}],
            "runner": {"type": "webhook", "url": "https://runner.example/"}}
    sid = (await client.post("/api/evaluations/sets", headers=auth_headers, json=body)).json()["id"]
    run = (await client.post(f"/api/evaluations/sets/{sid}/run", headers=auth_headers,
                             json={"agent_id": agent.id})).json()
    assert (run["agent_version"], run["score"], run["status"]) == ("1.0.0", 1.0, "completed")
    r = await client.post(f"/api/agents/{agent.id}/versions", headers=auth_headers,
                          json={"version": "2.0.0", "model_name": "m"})
    assert r.status_code == 200
    r = await client.post(f"/api/agents/{agent.id}/promote", headers=auth_headers,
                          json={"target_environment": "prod", "version": "2.0.0"})
    assert r.status_code == 400, "2.0.0 was never evaluated, yet promoted on 1.0.0's run"


# --- A15-5 (medium): the Slack callback is not bound to the admin who started it ---

@pytest.mark.xfail(strict=True, reason="A15-5: the Slack OAuth callback needs only the state, so whoever completes "
                                       "an admin's authorize URL lands their Slack workspace in that admin's org")
async def test_slack_callback_is_completed_only_by_the_admin_who_started_it(client, db, org, auth_headers,
                                                                            monkeypatch):
    monkeypatch.setattr(settings, "SLACK_CLIENT_ID", "cid")
    monkeypatch.setattr(settings, "SLACK_CLIENT_SECRET", "csecret")

    async def exchange(code):  # the workspace of whoever approved the URL in Slack
        return {"ok": True, "team": {"name": "Victim"}, "incoming_webhook": {"url": "https://hooks.slack.com/v"}}
    monkeypatch.setattr(integrations_router, "_slack_exchange", exchange)
    r = await client.get("/api/integrations/oauth/slack/start", headers=auth_headers)
    assert r.status_code == 200
    state = parse_qs(urlparse(r.json()["url"]).query)["state"][0]
    # the URL is sent to someone else, who approves it; their browser carries no AgentOps session of the starter
    r = await client.get("/api/integrations/oauth/slack/callback", params={"code": "c", "state": state})
    assert r.status_code in (401, 403), f"{r.status_code}: {r.text}"


# --- A15-6 (medium): /ws/traces outlives its access token ---

@pytest.mark.xfail(strict=True, reason="A15-6: /ws/traces checks the token only at connect, so a feed stays open "
                                       "past the token's expiry, a password change and every session's revocation")
def test_ws_feed_closes_once_its_token_expires(app_, org, viewer_user, monkeypatch, redis):
    monkeypatch.setattr(ws_router, "FEED_RECHECK_S", 0.05)
    exp = datetime.utcnow() + timedelta(seconds=2)
    token = jwt.encode({"sub": viewer_user.id, "org_id": org.id, "role": "viewer", "exp": exp, "type": "access"},
                       settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    closed = None
    with TestClient(app_) as tc, tc.websocket_connect(f"/ws/traces?token={token}") as sock:
        assert sock.receive_json() == {"type": "ping"}  # a member with a valid token
        time.sleep(3)
        with pytest.raises(Exception):
            decode_token(token)  # expired now
        try:
            for _ in range(40):
                sock.receive_json()
        except WebSocketDisconnect as e:
            closed = e.code
    assert closed == 4401


# --- A15-7 (medium): an account added to a second org can never sign in to it ---

@pytest.mark.xfail(strict=True, reason="A15-7: login signs in to one arbitrary membership and nothing switches org, "
                                       "so an existing account an invite 'added' can never act in that org")
async def test_an_added_account_can_sign_in_to_the_org_it_was_added_to(client, db, org, auth_headers):
    pw = "correct-horse-1"
    r = await client.post("/api/auth/register", json={"email": "bob@example.com", "password": pw})
    assert r.status_code == 200
    home = decode_token(r.json()["access_token"])["org_id"]
    r = await client.post("/api/auth/invite", headers=auth_headers, json={"email": "bob@example.com",
                                                                          "role": "developer"})
    assert r.status_code == 200 and r.json()["status"] == "added"
    r = await client.post("/api/auth/login", json={"email": "bob@example.com", "password": pw, "org_id": org.id})
    assert r.status_code == 200
    tok = r.json()["access_token"]
    me = (await client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"})).json()
    assert {o["id"] for o in me["organizations"]} == {home, org.id}  # a member of both
    assert decode_token(tok)["org_id"] == org.id


# --- A15-8 (medium): a set's schedule cannot be removed ---

@pytest.mark.xfail(strict=True, reason="A15-8: PATCH drops null fields, so schedule_cron cannot be removed, and no "
                                       "route deletes a set: a scheduled set runs (and calls its webhook) for good")
async def test_a_sets_schedule_can_be_removed(client, auth_headers):
    r = await client.post("/api/evaluations/sets", headers=auth_headers,
                          json={"name": "s", "schedule_cron": "0 * * * *", "cases": []})
    sid = r.json()["id"]
    r = await client.patch(f"/api/evaluations/sets/{sid}", headers=auth_headers, json={"schedule_cron": "5 * * * *"})
    assert r.status_code == 200 and r.json()["schedule_cron"] == "5 * * * *"  # a schedule can be changed
    r = await client.patch(f"/api/evaluations/sets/{sid}", headers=auth_headers, json={"schedule_cron": None})
    assert r.status_code == 200
    assert (await client.get(f"/api/evaluations/sets/{sid}", headers=auth_headers)).json()["schedule_cron"] is None


# --- A15-9 (medium): NAT64 addresses pass the public-address check ---

@pytest.mark.xfail(strict=True, reason="A15-9: is_public accepts 64:ff9b::/96 (NAT64), which reaches the embedded "
                                       "IPv4 address, private ones included, on a NAT64 network")
async def test_outbound_refuses_nat64_addresses_of_private_ipv4(monkeypatch):
    answer = []

    async def resolve(host, port):
        return answer
    monkeypatch.setattr(outbound, "_resolve", resolve)
    monkeypatch.setattr(outbound, "_transport", httpx.MockTransport(lambda r: httpx.Response(200)))

    async def blocked(addr):
        answer[:] = [addr]
        try:
            await outbound.post("https://h.example/", json={})
        except outbound.BlockedURL:
            return True
        return False
    assert not await blocked(PUBLIC_IP)
    assert await blocked("::ffff:169.254.169.254") and await blocked("169.254.169.254")
    assert await blocked("64:ff9b::a9fe:a9fe")  # 169.254.169.254 through NAT64


# --- A15-10 (medium): scheduled evaluation runs share the alert worker's queue ---

@pytest.mark.xfail(strict=True, reason="A15-10: evaluation runs go to the default queue with the alert tick, so one "
                                       "org's scheduled runs (up to 300 s each, unbounded per org) delay every org's "
                                       "alerts")
def test_evaluation_runs_do_not_share_the_alert_queue():
    def queue(name):
        return celery.amqp.router.route({}, name)["queue"].name
    assert queue("app.tasks.policy_tasks.check_trace") == "policy"  # routing works: policy checks have their own
    assert queue("app.tasks.evaluation_tasks.execute_run") != queue("app.tasks.alert_tasks.evaluate_alert_rules")
