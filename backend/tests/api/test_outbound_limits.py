"""SG-AP1.3: outbound replies are read up to a cap within a total deadline, and NAT64, IPv4-compatible and site-local
addresses are judged like the IPv4 inside them (or refused)."""
import asyncio
import gzip
import ipaddress
import json
import socket
import threading
import time

import httpx
import pytest

from app.models import Integration
from app.services import notification_service, outbound

PUBLIC_IP = "93.184.216.34"
CAP = outbound.MAX_REPLY_BYTES


class Chunks(httpx.AsyncByteStream):
    """`n` chunks of `size` bytes, `delay` seconds apart; counts what was sent."""

    def __init__(self, n, size, delay=0.0):
        self.n, self.size, self.delay, self.sent = n, size, delay, 0

    async def __aiter__(self):
        for _ in range(self.n):
            if self.delay:
                await asyncio.sleep(self.delay)
            self.sent += self.size
            yield b"x" * self.size


@pytest.fixture
def net(monkeypatch):
    """Every host resolves to `state["answer"]`; replies come from `state["reply"]` (default 200 {"output": "ok"})."""
    state = {"answer": [PUBLIC_IP], "reply": None, "requests": []}

    async def resolve(host, port):
        return state["answer"]

    async def handler(request):
        state["requests"].append(request)
        reply = state["reply"]
        return await reply(request) if reply else httpx.Response(200, json={"output": "ok"})
    monkeypatch.setattr(outbound, "_resolve", resolve)
    monkeypatch.setattr(outbound, "_transport", httpx.MockTransport(handler))
    return state


def _stream(state, stream, status=200, headers=None):
    async def reply(request):
        return httpx.Response(status, stream=stream, headers=headers)
    state["reply"] = reply
    return stream


def _trickle(state, delay=0.05):
    return _stream(state, Chunks(10_000, 1, delay))


# --- addresses ---

@pytest.mark.parametrize("ip", [
    "64:ff9b::a9fe:a9fe", "64:ff9b::a00:1", "64:ff9b::7f00:1", "64:ff9b::c0a8:101", "64:ff9b::6440:1",
    "64:ff9b::", "64:ff9b::e000:1", "64:ff9b:1::a00:1", "64:ff9b:1::5db8:d822",
    "::127.0.0.1", "::10.0.0.1", "::169.254.169.254", "::0.0.0.2", "::224.0.0.1",
    "fec0::1", "fed0::5db8:d822", "feff:ffff::1",
])
def test_nat64_ipv4_compatible_and_site_local_addresses_are_not_public(ip):
    assert not outbound.is_public(ipaddress.ip_address(ip))


@pytest.mark.parametrize("ip", ["64:ff9b::808:808", "64:ff9b::5db8:d822", "::8.8.8.8", "::93.184.216.34"])
def test_nat64_and_ipv4_compatible_addresses_of_public_ipv4_are_public(ip):
    assert outbound.is_public(ipaddress.ip_address(ip))


@pytest.mark.parametrize("answer", [["64:ff9b::a9fe:a9fe"], ["::127.0.0.1"], ["fec0::1"], [PUBLIC_IP, "64:ff9b::a00:1"]])
async def test_outbound_refuses_hosts_resolving_to_disguised_private_addresses(net, answer):
    net["answer"] = answer
    with pytest.raises(outbound.BlockedURL, match="blocked_address"):
        await outbound.post("https://hooks.example.com/x", json={})
    assert net["requests"] == []


@pytest.mark.parametrize("url", ["http://[64:ff9b::a9fe:a9fe]/", "http://[::127.0.0.1]/", "http://[fec0::1]/"])
async def test_outbound_refuses_disguised_private_literals(url, monkeypatch):
    sent = []
    monkeypatch.setattr(outbound, "_transport", httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200)))
    with pytest.raises(outbound.BlockedURL):
        await outbound.post(url, json={})
    assert sent == []


async def test_outbound_reaches_a_nat64_address_of_a_public_ipv4(net):
    net["answer"] = ["64:ff9b::5db8:d822"]
    r = await outbound.post("https://hooks.example.com/x", json={})
    assert r.status_code == 200 and str(net["requests"][0].url) == "https://[64:ff9b::5db8:d822]/x"


# --- the reply cap ---

async def test_a_reply_up_to_the_cap_is_read_whole(net):
    _stream(net, Chunks(4, CAP // 4))
    r = await outbound.post("https://hooks.example.com/x", json={})
    assert r.status_code == 200 and r.content == b"x" * CAP


async def test_a_reply_one_byte_over_the_cap_is_refused(net):
    _stream(net, Chunks(CAP + 1, 1))
    with pytest.raises(outbound.ReplyTooLarge, match="reply_too_large"):
        await outbound.post("https://hooks.example.com/x", json={})


async def test_an_endless_reply_is_read_only_to_the_cap(net):
    stream = _stream(net, Chunks(10_000, 4096))
    with pytest.raises(outbound.ReplyTooLarge):
        await outbound.post("https://hooks.example.com/x", json={})
    assert stream.sent <= CAP + 4096


async def test_an_error_status_reply_is_capped_too(net):
    stream = _stream(net, Chunks(10_000, 4096), status=500)
    with pytest.raises(outbound.ReplyTooLarge):
        await outbound.post("https://hooks.example.com/x", json={})
    assert stream.sent <= CAP + 4096


async def test_a_capped_reply_keeps_its_status_headers_and_json(net):
    async def reply(request):
        return httpx.Response(201, json={"output": "y" * 1000}, headers={"X-A": "1"})
    net["reply"] = reply
    r = await outbound.post("https://hooks.example.com/x", json={})
    assert (r.status_code, r.headers["x-a"], r.json()) == (201, "1", {"output": "y" * 1000})
    assert net["requests"][0].headers["accept-encoding"] == "identity"


async def test_a_compressed_reply_is_capped_by_its_bytes_on_the_wire_and_never_inflated(net):
    bomb = gzip.compress(b"x" * (64 * CAP))
    assert len(bomb) < CAP

    async def reply(request):
        return httpx.Response(200, content=bomb, headers={"Content-Encoding": "gzip"})
    net["reply"] = reply
    r = await outbound.post("https://hooks.example.com/x", json={})
    assert r.content == bomb and "content-encoding" not in r.headers


# --- the total deadline ---

async def test_a_trickled_reply_is_cut_at_the_deadline(net):
    stream = _trickle(net)
    t0 = time.perf_counter()
    with pytest.raises(httpx.TimeoutException):
        await outbound.post("https://hooks.example.com/x", json={}, timeout=0.3)
    assert time.perf_counter() - t0 < 1.5 and 0 < stream.sent < 100


async def test_a_reply_whose_headers_never_come_is_cut_at_the_deadline(net):
    async def never(request):
        await asyncio.sleep(30)
    net["reply"] = never
    t0 = time.perf_counter()
    with pytest.raises(httpx.TimeoutException):
        await outbound.post("https://hooks.example.com/x", json={}, timeout=0.3)
    assert time.perf_counter() - t0 < 1.5


async def test_a_slow_lookup_counts_toward_the_deadline(net, monkeypatch):
    async def slow(host, port):
        await asyncio.sleep(30)
    monkeypatch.setattr(outbound, "_resolve", slow)
    t0 = time.perf_counter()
    with pytest.raises((httpx.TimeoutException, outbound.BlockedURL)):
        await outbound.post("https://hooks.example.com/x", json={}, timeout=0.3)
    assert time.perf_counter() - t0 < 1.5


async def test_the_default_deadline_is_the_module_timeout(net, monkeypatch):
    monkeypatch.setattr(outbound, "TIMEOUT_S", 0.3)
    _trickle(net)
    t0 = time.perf_counter()
    with pytest.raises(httpx.TimeoutException):
        await outbound.post("https://hooks.example.com/x", json={})
    assert time.perf_counter() - t0 < 1.5


def _trickling_server(gap_s, count):
    """A real server that sends headers, then one body byte every `gap_s` seconds (each read is quick)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    stop = threading.Event()

    def serve():
        conn, _ = srv.accept()
        conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % count)
        for _ in range(count):
            if stop.wait(gap_s):
                break
            try:
                conn.sendall(b"x")
            except OSError:
                break
        conn.close()
        srv.close()
    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1], stop


async def test_a_real_trickling_server_is_cut_at_the_deadline_though_no_read_times_out(monkeypatch):
    port, stop = _trickling_server(0.05, 1000)

    async def resolve(host, p):
        return ["127.0.0.1"]
    monkeypatch.setattr(outbound, "_resolve", resolve)
    monkeypatch.setattr(outbound, "is_public", lambda ip: True)  # loopback stands in for a public server
    t0 = time.perf_counter()
    try:
        with pytest.raises(httpx.TimeoutException):
            await outbound.post(f"http://hooks.internal.test:{port}/in", json={}, timeout=0.5)
    finally:
        stop.set()
    assert time.perf_counter() - t0 < 2


# --- every caller ---

@pytest.mark.parametrize("kind,config", [("webhook", {"url": "https://hooks.example.com/x"}),
                                         ("slack", {"webhook_url": "https://hooks.example.com/x"})])
async def test_integration_test_fails_on_a_reply_over_the_cap(client, db, org, auth_headers, net, kind, config):
    stream = _stream(net, Chunks(10_000, 4096))
    i = Integration(org_id=org.id, kind=kind, name="I", config=config)
    db.add(i)
    await db.commit()
    r = await client.post(f"/api/integrations/{i.id}/test", headers=auth_headers)
    assert r.status_code == 200 and (r.json()["ok"], r.json()["message"]) == (False, "reply_too_large")
    assert stream.sent <= CAP + 4096


@pytest.mark.parametrize("kind,config", [("webhook", {"url": "https://hooks.example.com/x"}),
                                         ("slack", {"webhook_url": "https://hooks.example.com/x"})])
async def test_integration_test_fails_at_the_deadline_on_a_trickled_reply(client, db, org, auth_headers, net,
                                                                          monkeypatch, kind, config):
    monkeypatch.setattr(outbound, "TIMEOUT_S", 0.3)
    _trickle(net)
    i = Integration(org_id=org.id, kind=kind, name="I", config=config)
    db.add(i)
    await db.commit()
    t0 = time.perf_counter()
    r = await client.post(f"/api/integrations/{i.id}/test", headers=auth_headers)
    assert (r.json()["ok"], r.json()["message"]) == (False, "timeout") and time.perf_counter() - t0 < 2


async def test_alert_channels_fail_a_send_whose_reply_is_over_the_cap(net):
    stream = _stream(net, Chunks(10_000, 4096))
    channels = [{"type": "webhook", "target": "https://hooks.example.com/w"},
                {"type": "slack", "target": "https://hooks.example.com/s"}]
    assert await notification_service.dispatch_alert(channels, "m", "high") == [False, False]
    assert len(net["requests"]) == 2 and stream.sent <= 2 * (CAP + 4096)


async def test_alert_channels_fail_a_send_at_the_deadline(net, monkeypatch):
    monkeypatch.setattr(outbound, "TIMEOUT_S", 0.3)
    _trickle(net)
    t0 = time.perf_counter()
    channels = [{"type": "webhook", "target": "https://hooks.example.com/w"},
                {"type": "slack", "target": "https://hooks.example.com/s"}]
    assert await notification_service.dispatch_alert(channels, "m", "high") == [False, False]
    assert time.perf_counter() - t0 < 2


async def test_alert_channels_still_send_with_a_small_reply(net):
    channels = [{"type": "webhook", "target": "https://hooks.example.com/w"},
                {"type": "slack", "target": "https://hooks.example.com/s"}]
    assert await notification_service.dispatch_alert(channels, "m", "high") == [True, True]


async def _eval(client, headers, agent, runner):
    r = await client.post("/api/evaluations/sets", headers=headers,
                          json={"name": "s", "evaluators": ["exact"], "cases": [{"input": "q"}], "runner": runner})
    r = await client.post(f"/api/evaluations/sets/{r.json()['id']}/run", json={"agent_id": agent.id}, headers=headers)
    detail = (await client.get(f"/api/evaluations/runs/{r.json()['id']}", headers=headers)).json()
    (result,) = detail["results"]
    return result


RUNNER = {"type": "webhook", "url": "https://runner.example.com/"}


async def test_webhook_runner_fails_a_case_whose_json_reply_is_over_the_cap(client, auth_headers, agent, net):
    async def reply(request):
        return httpx.Response(200, json={"output": "y" * CAP})
    net["reply"] = reply
    result = await _eval(client, auth_headers, agent, RUNNER)
    assert (result["passed"], result["reason"]) == (False, "runner_error: reply_too_large")


async def test_webhook_runner_reads_an_endless_reply_only_to_the_cap(client, auth_headers, agent, net):
    stream = _stream(net, Chunks(10_000, 4096))
    result = await _eval(client, auth_headers, agent, RUNNER)
    assert result["reason"] == "runner_error: reply_too_large" and stream.sent <= CAP + 4096


async def test_webhook_runner_takes_an_output_just_under_the_cap(client, auth_headers, agent, net):
    output = "y" * (CAP - len(json.dumps({"output": ""})))

    async def reply(request):
        return httpx.Response(200, content=json.dumps({"output": output}).encode())
    net["reply"] = reply
    result = await _eval(client, auth_headers, agent, RUNNER)
    assert result["reason"] != "runner_error: reply_too_large" and result["actual_output"] == output


async def test_webhook_runner_fails_a_trickled_reply_at_its_timeout(client, auth_headers, agent, net):
    _trickle(net)
    t0 = time.perf_counter()
    result = await _eval(client, auth_headers, agent, {**RUNNER, "timeout_s": 0.3})
    assert result["reason"] == "runner_error: timeout" and time.perf_counter() - t0 < 2
