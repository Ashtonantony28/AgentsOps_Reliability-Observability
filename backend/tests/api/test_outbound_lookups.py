"""SG-AP2.2: outbound lookups run on their own executor with a bounded queue, never the default executor, and an org's
outbound calls in flight are bounded, each until its lookup's thread has ended."""
import asyncio
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app.models import Integration
from app.services import outbound
from tests.api.test_outbound_limits import RUNNER, _eval
from tests.api.test_violation_pipeline import NO_DROP, _add, _policy_row, _rule, _stream

PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
URL = "https://hooks.example.com/x"


@pytest.fixture
def dns(monkeypatch):
    """socket.getaddrinfo answers PUBLIC for `.example` names (others, the database's, resolve as usual); names in
    `state["hold"]` wait for `state["release"]`. Records each lookup's thread name. At teardown, releases held lookups
    and checks every place was given back."""
    state = {"hold": set(), "release": threading.Event(), "threads": [], "hosts": []}
    real = socket.getaddrinfo

    def getaddrinfo(host, *a, **kw):
        if ".example" not in str(host):
            return real(host, *a, **kw)
        state["threads"].append(threading.current_thread().name)
        state["hosts"].append(host)
        if host in state["hold"]:
            state["release"].wait(10)
        return PUBLIC
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(outbound, "_transport", httpx.MockTransport(lambda r: httpx.Response(200)))
    yield state
    state["release"].set()
    for _ in range(200):
        if not outbound._lookups and not outbound._in_flight:
            break
        time.sleep(0.01)
    assert (outbound._lookups, dict(outbound._in_flight)) == (0, {})


async def _settle():
    for _ in range(200):
        if not outbound._lookups:
            return
        await asyncio.sleep(0.01)


def _as(org_id):
    outbound.org.set(org_id)


# --- the resolver's own executor ---

async def test_lookups_run_on_the_resolver_threads(dns):
    assert (await outbound.post(URL, json={})).status_code == 200
    assert dns["hosts"] == ["hooks.example.com"] and dns["threads"][0].startswith("outbound-resolve")


async def test_a_lookup_does_not_wait_for_a_full_default_executor(dns):
    loop = asyncio.get_running_loop()
    busy = threading.Event()
    loop.set_default_executor(ThreadPoolExecutor(1))
    blocker = loop.run_in_executor(None, busy.wait, 5)
    t0 = time.perf_counter()
    try:
        assert (await outbound.post(URL, json={}, timeout=1)).status_code == 200
    finally:
        busy.set()
        await blocker
    assert time.perf_counter() - t0 < 0.5


async def test_an_abandoned_lookup_keeps_its_place_until_its_thread_ends(dns, monkeypatch):
    monkeypatch.setattr(outbound, "RESOLVE_TIMEOUT_S", 0.1)
    dns["hold"].add("slow.example")
    with pytest.raises(outbound.BlockedURL, match="unresolvable_host"):
        await outbound.post("https://slow.example/", json={})
    assert outbound._lookups == 1
    dns["release"].set()
    await _settle()
    assert outbound._lookups == 0


async def test_past_the_queue_a_lookup_is_refused_as_unresolvable_without_a_thread(dns, monkeypatch):
    monkeypatch.setattr(outbound, "RESOLVE_QUEUE", 2)
    dns["hold"].add("slow.example")
    held = [asyncio.create_task(outbound.post("https://slow.example/", json={}, timeout=5)) for _ in range(2)]
    await asyncio.sleep(0.05)
    t0 = time.perf_counter()
    with pytest.raises(outbound.BlockedURL, match="unresolvable_host"):
        await outbound.post(URL, json={})
    assert time.perf_counter() - t0 < 0.1 and dns["hosts"] == ["slow.example"] * 2
    dns["release"].set()
    assert [r.status_code for r in await asyncio.gather(*held)] == [200, 200]
    await _settle()
    assert (await outbound.post(URL, json={})).status_code == 200  # places given back


async def test_the_queue_counts_lookups_waiting_for_a_thread(dns, monkeypatch):
    monkeypatch.setattr(outbound, "_resolver", ThreadPoolExecutor(1))
    monkeypatch.setattr(outbound, "RESOLVE_QUEUE", 2)
    dns["hold"].add("slow.example")
    first = asyncio.create_task(outbound.post("https://slow.example/", json={}, timeout=5))
    await asyncio.sleep(0.05)
    waiting = asyncio.create_task(outbound.post(URL, json={}, timeout=5))
    await asyncio.sleep(0.05)
    assert outbound._lookups == 2 and dns["hosts"] == ["slow.example"]
    with pytest.raises(outbound.BlockedURL, match="unresolvable_host"):
        await outbound.post(URL, json={})
    waiting.cancel()  # cancelled before its thread ran: its place is freed at once
    await asyncio.gather(waiting, return_exceptions=True)
    assert outbound._lookups == 1
    dns["release"].set()
    assert (await first).status_code == 200


# --- calls in flight per org ---

@pytest.fixture
def slow_replies(dns, monkeypatch):
    gate = asyncio.Event()

    async def handler(request):
        await gate.wait()
        return httpx.Response(200)
    monkeypatch.setattr(outbound, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(outbound, "ORG_CALLS", 2)
    yield gate
    gate.set()


async def _in_flight(n, org_id):
    _as(org_id)
    tasks = [asyncio.create_task(outbound.post(URL, json={})) for _ in range(n)]
    await asyncio.sleep(0.05)
    return tasks


async def test_an_orgs_calls_past_the_bound_are_refused(slow_replies):
    tasks = await _in_flight(2, "org-a")
    with pytest.raises(outbound.BlockedURL, match="too_many_calls"):
        await outbound.post(URL, json={})
    slow_replies.set()
    assert [r.status_code for r in await asyncio.gather(*tasks)] == [200, 200]
    assert (await outbound.post(URL, json={})).status_code == 200


async def test_another_org_and_unscoped_calls_are_not_refused(slow_replies):
    tasks = await _in_flight(2, "org-a")
    tasks += await _in_flight(2, "org-b")
    tasks += await _in_flight(3, None)
    assert dict(outbound._in_flight) == {"org-a": 2, "org-b": 2}
    slow_replies.set()
    assert len(await asyncio.gather(*tasks)) == 7


async def test_an_orgs_place_is_held_until_its_abandoned_lookup_ends(dns, monkeypatch):
    monkeypatch.setattr(outbound, "ORG_CALLS", 1)
    monkeypatch.setattr(outbound, "RESOLVE_TIMEOUT_S", 0.1)
    dns["hold"].add("slow.example")
    _as("org-a")
    with pytest.raises(outbound.BlockedURL, match="unresolvable_host"):
        await outbound.post("https://slow.example/", json={})
    with pytest.raises(outbound.BlockedURL, match="too_many_calls"):
        await outbound.post(URL, json={})
    dns["release"].set()
    await _settle()
    assert (await outbound.post(URL, json={})).status_code == 200


@pytest.mark.parametrize("case", ["blocked_address", "invalid_url", "reply_too_large", "timeout", "raises"])
async def test_an_orgs_place_is_given_back_whatever_the_call_ends_with(dns, monkeypatch, case):
    monkeypatch.setattr(outbound, "ORG_CALLS", 1)
    url = "ftp://x/" if case == "invalid_url" else URL
    if case == "blocked_address":
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(2, 1, 6, "", ("10.0.0.1", 443))])

    async def handler(request):
        if case == "timeout":
            await asyncio.sleep(5)
        if case == "raises":
            raise httpx.ConnectError("refused")
        return httpx.Response(200, content=b"x" * (outbound.MAX_REPLY_BYTES + 1))
    monkeypatch.setattr(outbound, "_transport", httpx.MockTransport(handler))
    _as("org-a")
    with pytest.raises((outbound.BlockedURL, outbound.ReplyTooLarge, httpx.HTTPError)):
        await outbound.post(url, json={}, timeout=0.2)
    await _settle()
    assert not outbound._in_flight


# --- callers set the org ---

async def test_the_integration_test_is_bounded_per_org(client, db, org, auth_headers, slow_replies, monkeypatch):
    monkeypatch.setattr(outbound, "ORG_CALLS", 1)
    i = Integration(org_id=org.id, kind="webhook", name="I", config={"url": URL})
    db.add(i)
    await db.commit()
    first = asyncio.create_task(client.post(f"/api/integrations/{i.id}/test", headers=auth_headers))
    await asyncio.sleep(0.1)
    try:
        assert dict(outbound._in_flight) == {org.id: 1}
        r = await client.post(f"/api/integrations/{i.id}/test", headers=auth_headers)
        assert (r.json()["ok"], r.json()["message"]) == (False, "too_many_calls")
    finally:
        slow_replies.set()
        first = await first
    assert first.json()["ok"] is True


@pytest.fixture
def orgs_seen(monkeypatch):
    seen = []
    real = outbound.post

    async def post(url, **kw):
        seen.append(outbound.org.get())
        return await real(url, **kw)
    monkeypatch.setattr(outbound, "post", post)
    return seen


async def test_the_eval_runner_calls_as_the_sets_org(client, org, auth_headers, agent, dns, orgs_seen):
    await _eval(client, auth_headers, agent, RUNNER)
    assert orgs_seen == [org.id]


async def test_violation_alert_channels_call_as_the_rules_org(client, db, org, agent, api_key_headers, dns, orgs_seen):
    from app.services import violation_service
    rule = _rule(org, severity="critical")
    rule.channels = [{"type": "webhook", "target": URL}, {"type": "slack", "target": URL}]
    await _add(db, rule, _policy_row(org, NO_DROP))
    await _stream(client, api_key_headers, agent, "u1", "drop_db")
    await violation_service.drain_notifications()
    assert orgs_seen == [org.id, org.id]
