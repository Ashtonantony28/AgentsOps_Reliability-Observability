"""AUDIT-18 (platform after SG-AP3.1/SG-AP3.2: the alert tick checked in turns by org from `_resume_at` with the
200-rule cap and the per-rule fire lock; outbound lookups one thread per queue place, ORG_LOOKUPS/ORG_CALLS via
`_Places`, `/test` limited per org, `outbound.org` set per dispatch task).

This audit found nothing above low, so there are no xfail(strict=True) findings tests here. These are regression
guards: each confirms an SG-AP3 fix defends the AUDIT-17 attack from a fresh angle and fails if the fix is reverted.
They run on SQLite and on Postgres (`TEST_DB=postgres`). The two low findings (L18-1, L18-2) are repro descriptions
in AUDIT-18.md, not tests."""
import asyncio
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta

import httpx
import pytest

from app.middleware.auth import make_access_token
from app.models import AlertEvent, AlertRule, Integration, Organization, OrgMembership, User
from app.schemas.alert import MAX_ORG_RULES
from app.services import alert_service, outbound
from app.services.alert_service import evaluate_metric_rules

PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


@pytest.fixture
def dns(monkeypatch):
    """`.example` names resolve to a public address, except `slow-dns.example`, whose lookups hold their resolver
    thread (a name server that never answers) until the test ends. Requests go to a mock transport; `sent` lists the
    hosts that were actually posted to."""
    state = {"sent": [], "blocked": 0, "release": threading.Event()}
    real = socket.getaddrinfo

    def getaddrinfo(host, *a, **kw):
        if host == "slow-dns.example":
            state["blocked"] += 1
            state["release"].wait(10)
            raise socket.gaierror("timeout")
        return PUBLIC if ".example" in str(host) else real(host, *a, **kw)

    async def handler(request):
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


def _firing(org, name, created_at=None):
    return AlertRule(org_id=org.id, name=name, metric="token_usage", operator=">=", threshold=0, window_minutes=60,
                     created_at=created_at or datetime.utcnow(),
                     channels=[{"type": "webhook", "target": f"https://{name}.example/h"}])


# --- SG-AP3.1 regression: a third org's rule is checked within a bounded number of deadline-limited ticks ---

async def test_turns_check_a_small_orgs_rule_despite_a_huge_orgs_rules(db, session_maker, org, dns, monkeypatch):
    """A17-1 defence: with the check deadline reached every tick, the turn order plus `_resume_at` still reaches a
    small org's rule within a couple of ticks, however many rules a huge org has."""
    monkeypatch.setattr(alert_service, "CHECK_DEADLINE_S", 0.25)
    old = datetime.utcnow() - timedelta(hours=1)
    db.add_all([AlertRule(org_id=org.id, name=f"big{i}", metric="error_rate", operator=">", threshold=2,
                          window_minutes=1, created_at=old) for i in range(3000)])
    victim, _ = await _org_with_admin(db, "V")
    db.add(_firing(victim, "victim", created_at=datetime.utcnow()))  # newest of all
    await db.commit()
    for _ in range(3):  # a small, bounded number of ticks
        async with session_maker() as s:
            await evaluate_metric_rules(s)
        if "victim.example" in dns["sent"]:
            break
    assert "victim.example" in dns["sent"], "turn order did not reach the small org's rule"


# --- SG-AP3.2 regression: one org cannot hold more than ORG_LOOKUPS resolver-queue places ---

async def test_one_org_holds_at_most_org_lookups_queue_places(dns, monkeypatch):
    """A17-2 defence: an org firing many slow lookups occupies at most ORG_LOOKUPS of the RESOLVE_QUEUE, so filling
    the queue takes RESOLVE_QUEUE/ORG_LOOKUPS orgs, not two."""
    monkeypatch.setattr(outbound, "RESOLVE_TIMEOUT_S", 0.3)
    outbound.org.set("hog")
    calls = [asyncio.create_task(outbound.post("https://slow-dns.example/h", json={})) for _ in range(8)]
    for _ in range(200):
        if outbound._lookups >= outbound.ORG_LOOKUPS:
            break
        await asyncio.sleep(0.01)
    running = outbound._lookups
    results = await asyncio.gather(*calls, return_exceptions=True)
    assert running == outbound.ORG_LOOKUPS, f"one org held {running} resolver places, expected {outbound.ORG_LOOKUPS}"
    assert all(isinstance(r, outbound.BlockedURL) for r in results)


# --- SG-AP3.1 regression: the per-org rule cap ---

async def test_rule_cap_is_per_org_and_counts_all_rule_kinds(client, db, org, auth_headers):
    """SG-AP3.1 cap: MAX_ORG_RULES per org (disabled and policy_violations rules counted), 409 past it, and another
    org is unaffected."""
    assert MAX_ORG_RULES == 200  # the approved contract value
    db.add_all([AlertRule(org_id=org.id, name=f"r{i}", metric="policy_violations", operator=">", threshold=0,
                          enabled=(i % 2 == 0)) for i in range(MAX_ORG_RULES - 1)])
    await db.commit()
    body = {"name": "x", "metric": "error_rate", "operator": ">", "threshold": 1}
    assert (await client.post("/api/alerts/rules", headers=auth_headers, json=body)).status_code == 200  # 200th
    r = await client.post("/api/alerts/rules", headers=auth_headers, json=body)  # 201st
    assert r.status_code == 409 and r.json()["error"]["code"] == "rule_limit"
    other, oheaders = await _org_with_admin(db, "Other")
    assert (await client.post("/api/alerts/rules", headers=oheaders, json=body)).status_code == 200  # per org


# --- SG-AP3.1 regression: a rule to fire is fired once under its lock + dedup re-check ---

async def test_rechecked_dedup_fires_a_rule_once(db, session_maker, org, dns):
    """A17-4 defence: a rule already firing (within dedup) in a committed event is not paged again by the tick."""
    rule = _firing(org, "once")
    db.add(rule)
    await db.flush()
    db.add(AlertEvent(rule_id=rule.id, org_id=org.id, status="firing", value=1.0, message="prior",
                      fired_at=datetime.utcnow()))
    await db.commit()
    async with session_maker() as s:
        summary = await evaluate_metric_rules(s)
    assert summary["fired"] == 0 and "once.example" not in dns["sent"]


# --- SG-AP3.2 regression: /test is rate limited per org ---

async def test_integration_test_rate_limit_is_per_org(client, db, org, auth_headers, redis, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "INTEGRATION_TEST_LIMIT_PER_MIN", 3)
    i = Integration(org_id=org.id, kind="webhook", name="w", config={"url": "https://x.example/"})
    db.add(i)
    await db.commit()
    codes = [(await client.post(f"/api/integrations/{i.id}/test", headers=auth_headers)).status_code for _ in range(4)]
    assert codes.count(429) == 1 and codes[-1] == 429
    other, oheaders = await _org_with_admin(db, "O2")
    j = Integration(org_id=other.id, kind="webhook", name="w", config={"url": "https://x.example/"})
    db.add(j)
    await db.commit()
    assert (await client.post(f"/api/integrations/{j.id}/test", headers=oheaders)).status_code != 429  # separate org
