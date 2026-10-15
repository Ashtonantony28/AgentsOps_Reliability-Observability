import asyncio
import logging

import httpx
import pytest

import agentops_sdk
from agentops_sdk import CheckResult, PolicyBlocked
from agentops_sdk.client import AgentOpsClient

VIOLATION = {"id": "v1", "trace_id": None, "agent_id": "a1", "policy_id": "p1", "policy_name": "no-delete",
             "policy_version": 1, "severity": "high", "at_seq": 0, "counterexample": [], "message": "nope",
             "mode": "precheck", "detected_at": "2026-01-01T00:00:00"}

FAILURES = [
    (httpx.ConnectError("refused"), "ConnectError"),
    (httpx.ReadTimeout("slow"), "timeout"),
    ((500, {"error": {"code": "internal_error"}}), "http 500"),
    ((429, {"error": {"code": "rate_limited"}}), "http 429"),
    ((409, {"error": {"code": "agent_mismatch"}}), "http 409"),
    ((200, "not json"), "malformed response"),
    ((200, {"violations": []}), "malformed response"),
    ((200, {"allowed": "yes", "violations": []}), "malformed response"),
    ((200, {"allowed": True, "violations": "x"}), "malformed response"),
]


def test_check_allowed_request_shape(client, server):
    r = client.check("send_email", input={"to": "x"}, recipients=3)
    assert r == CheckResult(allowed=True, violations=[], error=None)
    (path, body, headers), = server.requests
    assert path == "/api/trace-policies/check"
    assert headers["x-api-key"] == client.api_key
    assert body == {"agent_id": "a1", "trace_uid": None, "environment": "dev", "prefix": [], "candidate": {
        "seq": 0, "kind": "tool", "name": "send_email", "status": "success", "input": {"to": "x"},
        "output": {}, "attrs": {"recipients": 3}, "latency_ms": 0, "timestamp": None}}
    assert server.timeouts == [2.0]


def test_check_blocked(client, server):
    server.queue("check", (200, {"allowed": False, "violations": [VIOLATION]}))
    r = client.check("delete_db", kind="custom")
    assert r.allowed is False and r.violations == [VIOLATION] and r.error is None
    assert server.bodies("check")[0]["candidate"]["kind"] == "custom"


@pytest.mark.parametrize("reply,reason", FAILURES)
def test_check_fail_open(client, server, caplog, reply, reason):
    server.queue("check", reply)
    with caplog.at_level(logging.WARNING, logger="agentops_sdk"):
        r = client.check("x")
    assert r.allowed is True and r.violations == [] and reason in r.error
    assert "policy check failed" in caplog.text


@pytest.mark.parametrize("status", [401, 403, 404, 413, 422])
def test_check_config_errors_block_both_clients(client, aclient, server, caplog, status):
    for c in (client, aclient):
        server.queue("check", (status, {"error": {"code": "http_error", "message": "invalid_api_key"}}))
        with caplog.at_level(logging.ERROR, logger="agentops_sdk"):
            r = c.check("x") if c is client else asyncio.run(c.acheck("x"))
        assert r == CheckResult(allowed=False, error=f"http {status}")
    assert [rec.levelname for rec in caplog.records] == ["ERROR", "ERROR"]
    assert "invalid_api_key" in caplog.text


@pytest.mark.parametrize("reply,reason", FAILURES)
def test_check_fail_closed(make_client, server, reply, reason):
    c = make_client(fail_closed=True, check_timeout_s=0.5)
    server.queue("check", reply)
    r = c.check("x")
    assert r.allowed is False and r.violations == [] and reason in r.error
    assert server.timeouts == [0.5]


@pytest.mark.parametrize("reply,reason", FAILURES[:3])
def test_acheck_fail_open_and_closed(make_client, make_async_client, server, reply, reason):
    for c, allowed in ((make_client(), True), (make_async_client(fail_closed=True), False)):
        server.queue("check", reply)
        r = asyncio.run(c.acheck("x"))
        assert r.allowed is allowed and reason in r.error


def test_acheck_both_clients(client, aclient, server):
    server.queue("check", (200, {"allowed": False, "violations": [VIOLATION]}))
    assert asyncio.run(client.acheck("x", input={"a": 1})).allowed is False
    assert asyncio.run(aclient.acheck("y", kind="llm")).allowed is True
    assert aclient.check("z").allowed is True
    names = [b["candidate"]["name"] for b in server.bodies("check")]
    assert names == ["x", "y", "z"]
    assert server.bodies("check")[0]["candidate"]["input"] == {"a": 1}


def test_module_check_requires_init(singleton):
    with pytest.raises(RuntimeError):
        agentops_sdk.check("x")
    with pytest.raises(RuntimeError):
        asyncio.run(agentops_sdk.acheck("x"))


def test_module_check_uses_singleton(singleton, server):
    c = singleton(fail_closed=True)
    server.queue("check", httpx.ConnectError("down"))
    assert agentops_sdk.check("x").allowed is False
    assert asyncio.run(agentops_sdk.acheck("y", foo=1)).allowed is True
    assert server.bodies("check")[1]["candidate"]["attrs"] == {"foo": 1}
    assert server.requests[0][2]["x-api-key"] == c.api_key


def test_init_passes_check_options(monkeypatch, server):
    from agentops_sdk import client as client_mod
    monkeypatch.setattr(client_mod, "_singleton", None)
    c = agentops_sdk.init(api_key="tinit-" + "x" * 20, agent_id="a1", flush_interval_s=3600,
                          fail_closed=True, check_timeout_s=0.25)
    c._stop.set()
    monkeypatch.setattr(client_mod, "_singleton", None)
    assert (c.fail_closed, c.check_timeout_s) == (True, 0.25)
    assert isinstance(c, AgentOpsClient)


def test_unstreamed_trace_check_sends_its_events_as_prefix(client, server):
    with client.trace() as t:
        t.add_event("tool", "a", input={"secret": 1}, region="eu")
        t.add_event("llm", "b", status="error")
        t.check("c")
    body = server.bodies("check")[0]
    assert body["trace_uid"] is None and body["candidate"]["seq"] == 2
    assert [(e["seq"], e["kind"], e["name"], e["status"], e["attrs"], e["input"]) for e in body["prefix"]] == [
        (0, "tool", "a", "success", {"region": "eu"}, {}), (1, "llm", "b", "error", {}, {})]
    assert server.bodies("events") == []


def test_unstreamed_tool_call_trace_check_sends_them_as_prefix(client, server):
    with client.trace() as t:
        t.add_tool_call("a", input={"q": 1}, status="error")
        t.check("b")
    body = server.bodies("check")[0]
    assert [(e["seq"], e["kind"], e["name"], e["status"], e["input"]) for e in body["prefix"]] == [
        (0, "tool", "a", "error", {})]
    assert body["candidate"]["seq"] == 1


def test_streamed_trace_check_sends_no_prefix(client, server):
    with client.trace(stream=True) as t:
        t.add_event("tool", "a")
        t.check("b")
    assert server.bodies("check")[0]["prefix"] == []


def test_trace_without_client_check_raises():
    from agentops_sdk import Trace
    with pytest.raises(RuntimeError):
        Trace(agent_id="a1").check("x")


def test_policy_blocked_message():
    e = PolicyBlocked(CheckResult(False, [VIOLATION, {**VIOLATION, "policy_name": "pii"}, VIOLATION]))
    assert str(e) == "blocked by policy: no-delete, pii"
    assert e.result.allowed is False
    assert "timeout" in str(PolicyBlocked(CheckResult(False, error="timeout")))
