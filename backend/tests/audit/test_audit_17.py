"""AUDIT-17 (platform after SG-AP2.1 to SG-AP2.3: alert tick isolation, outbound lookups, password hashing).

Each test is xfail(strict=True): it fails today at its final assertion, after its control assertions pass, and will
XPASS (failing CI) once its finding is fixed, so the fixing sub-goal removes the marker."""
import asyncio
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta

import httpx
import pytest

from app.middleware.auth import make_access_token
from app.models import AlertRule, Integration, Organization, OrgMembership, User
from app.services import alert_service, outbound, violation_service
from app.services.alert_service import evaluate_metric_rules
from tests.api.test_violation_pipeline import NO_DROP, _add, _policy_row, _rule, _stream

PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


@pytest.fixture
def dns(monkeypatch):
    """`.example` names resolve to a public address, except `slow-dns.example`, whose lookups hold their thread (a
    name server that does not answer) until the test ends. Requests go to a mock transport; `sent` lists their hosts,
    and a host in `held` is answered only at the test's end."""
    state = {"sent": [], "blocked": 0, "held": set(), "release": threading.Event()}
    real = socket.getaddrinfo

    def getaddrinfo(host, *a, **kw):
        if host == "slow-dns.example":
            state["blocked"] += 1
            state["release"].wait(10)
            raise socket.gaierror("timeout")
        return PUBLIC if ".example" in str(host) else real(host, *a, **kw)

    async def handler(request):
        if request.headers["host"] in state["held"]:
            while not state["release"].is_set():
                await asyncio.sleep(0.01)
        state["sent"].append(request.headers["host"])
        return httpx.Response(200)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(outbound, "_transport", httpx.MockTransport(handler))
    yield state
    state["release"].set()
    for _ in range(300):
        if not outbound._lookups and not outbound._in_flight:
            break
        time.sleep(0.01)


async def _org_with_admin(db, name):
    o = Organization(name=name, slug=f"{name.lower()}-{uuid.uuid4().hex[:8]}")
    u = User(email=f"{name.lower()}-{uuid.uuid4().hex[:8]}@example.com", password_hash="x", full_name=name)
    db.add_all([o, u])
    await db.flush()
    db.add(OrgMembership(org_id=o.id, user_id=u.id, role="admin"))
    await db.commit()
    return o, {"Authorization": f"Bearer {make_access_token(u.id, o.id, 'admin')}"}


# --- A17-1 (high): one org's rules keep every later rule of every org from being checked ---

@pytest.mark.xfail(strict=True, reason="A17-1: the check phase stops at CHECK_DEADLINE_S and always starts from the "
                                       "oldest rule, so one org's many rules keep every newer rule from being checked")
async def test_one_orgs_many_rules_do_not_keep_another_orgs_rule_unchecked(client, db, session_maker, org,
                                                                          auth_headers, dns, monkeypatch):
    monkeypatch.setattr(alert_service, "CHECK_DEADLINE_S", 0.3)  # 180 s, and each check costs ~60 ms on 100k traces
    never = {"name": "a", "metric": "error_rate", "operator": ">", "threshold": 2, "window_minutes": 1}
    assert (await client.post("/api/alerts/rules", headers=auth_headers, json=never)).status_code == 200  # no cap
    old = datetime.utcnow() - timedelta(hours=1)
    db.add_all([AlertRule(org_id=org.id, created_at=old, **never) for _ in range(3000)])
    other, _ = await _org_with_admin(db, "B")
    db.add(AlertRule(org_id=other.id, name="b", metric="token_usage", operator=">=", threshold=0,
                     channels=[{"type": "webhook", "target": "https://b.example/hook", "name": ""}]))
    await db.commit()
    for _ in range(3):  # three ticks
        async with session_maker() as s:
            summary = await evaluate_metric_rules(s)
        assert 0 < summary["checked"] < 3002  # each tick stopped at its deadline
    assert dns["sent"] == ["b.example"], "B's rule was never checked in three ticks"


# --- A17-2 (high): slow lookups of two orgs hold every resolver thread, and other orgs' pages fail ---

@pytest.mark.xfail(strict=True, reason="A17-2: two orgs' lookups of a name that never answers hold all RESOLVE_THREADS "
                                       "threads past their deadline, so every other org's lookups time out")
async def test_two_orgs_slow_lookups_do_not_fail_another_orgs_alert_page(client, db, org, agent, api_key_headers, dns,
                                                                         monkeypatch):
    monkeypatch.setattr(outbound, "RESOLVE_TIMEOUT_S", 0.2)
    rule = _rule(org, severity="critical")
    rule.channels = [{"type": "webhook", "target": "https://victim.example/hook"}]
    await _add(db, rule, _policy_row(org, NO_DROP))
    tests = []
    for name in ("X", "Y"):  # two self-registered orgs, one address's registrations
        o, headers = await _org_with_admin(db, name)
        i = Integration(org_id=o.id, kind="webhook", name="w", config={"url": "https://slow-dns.example/"})
        await _add(db, i)
        tests += [(i.id, headers)] * outbound.ORG_CALLS
    answers = await asyncio.gather(*(client.post(f"/api/integrations/{iid}/test", headers=h) for iid, h in tests))
    assert {(r.status_code, r.json()["message"]) for r in answers} == {(200, "unresolvable_host")}  # answered
    assert dns["blocked"] and outbound._lookups == dns["blocked"]  # their threads are still held
    await _stream(client, api_key_headers, agent, "u1", "drop_db")
    await violation_service.drain_notifications()
    assert dns["sent"] == ["victim.example"], "the victim org's alert page failed as unresolvable_host"


# --- A17-3 (medium): a rule at the channel bound loses a page while any other call of its org is in flight ---

@pytest.mark.xfail(strict=True, reason="A17-3: ORG_CALLS (10) equals MAX_CHANNELS and counts whole calls, so a rule's "
                                       "10th channel is refused as too_many_calls while another call of the org runs")
async def test_a_rule_at_the_channel_bound_is_paged_while_an_integration_test_runs(client, db, org, agent, auth_headers,
                                                                                  api_key_headers, dns):
    dns["held"].add("slow-reply.example")  # a receiver that answers slowly, as webhooks may
    rule = _rule(org, severity="critical")
    rule.channels = [{"type": "webhook", "target": f"https://hook{n}.example/"} for n in range(10)]  # the bound
    i = Integration(org_id=org.id, kind="webhook", name="w", config={"url": "https://slow-reply.example/"})
    await _add(db, rule, i, _policy_row(org, NO_DROP))
    test = asyncio.create_task(client.post(f"/api/integrations/{i.id}/test", headers=auth_headers))
    for _ in range(100):
        if outbound._in_flight.get(org.id):
            break
        await asyncio.sleep(0.01)
    assert outbound._in_flight[org.id] == 1  # the test's call, waiting for its reply
    await _stream(client, api_key_headers, agent, "u1", "drop_db")
    await violation_service.drain_notifications()
    dns["release"].set()
    assert (await test).json()["ok"] is True
    paged = sorted(h for h in dns["sent"] if h.startswith("hook"))
    assert len(paged) == 10, f"{10 - len(paged)} of the rule's 10 channels refused"
