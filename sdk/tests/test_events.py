import asyncio

import pytest

from agentops_sdk.precheck import check_request, event_dict, events_request
from agentops_sdk.trace import Trace, current_trace

LEGACY_KEYS = {"trace_id", "agent_id", "session_id", "input", "output", "status", "latency_ms", "tokens_input",
               "tokens_output", "cost_usd", "environment", "agent_version", "error_message", "tool_calls",
               "metadata", "timestamp"}


def test_legacy_payload_unchanged():
    t = Trace(agent_id="a1", input="hi")
    t.add_tool_call("search", input={"q": 1}, output={"n": 2}, latency_ms=5)
    p = t.to_payload()
    assert set(p) == LEGACY_KEYS
    assert p["tool_calls"] == [{"name": "search", "input": {"q": 1}, "output": {"n": 2},
                                "latency_ms": 5, "status": "success"}]
    assert set(Trace(agent_id="a1").to_payload()) == LEGACY_KEYS


def test_events_payload():
    t = Trace(agent_id="a1")
    t.add_event("tool", "search", input={"q": "x"}, output={"hits": 1}, source="web", latency_ms=12)
    t.add_event("llm", "answer", status="error")
    p = t.to_payload()
    assert set(p) == LEGACY_KEYS | {"events"}
    assert p["tool_calls"] == []
    e0, e1 = p["events"]
    assert [e0["seq"], e1["seq"]] == [0, 1]
    assert e0 == {"seq": 0, "kind": "tool", "name": "search", "status": "success", "input": {"q": "x"},
                  "output": {"hits": 1}, "attrs": {"source": "web"}, "latency_ms": 12,
                  "timestamp": e0["timestamp"]}
    assert isinstance(e0["timestamp"], str)
    assert (e1["kind"], e1["status"], e1["input"], e1["attrs"]) == ("llm", "error", {}, {})


def test_mixing_raises():
    t = Trace(agent_id="a1")
    t.add_tool_call("x")
    with pytest.raises(ValueError):
        t.add_event("tool", "y")
    t = Trace(agent_id="a1")
    t.add_event("tool", "y")
    with pytest.raises(ValueError):
        t.add_tool_call("x")
    assert len(t.events) == 1 and t.tool_calls == []


def test_unknown_kind_raises():
    with pytest.raises(ValueError):
        Trace(agent_id="a1").add_event("weird", "x")


def test_request_builders():
    assert event_dict(3, "retrieval", "docs") == {
        "seq": 3, "kind": "retrieval", "name": "docs", "status": "success", "input": {}, "output": {},
        "attrs": {}, "latency_ms": 0, "timestamp": None}
    cand = event_dict(0, "tool", "x")
    assert check_request("a1", None, cand, "prod") == {
        "agent_id": "a1", "trace_uid": None, "environment": "prod", "candidate": cand}
    assert events_request("a1", [cand], True) == {"agent_id": "a1", "events": [cand], "final": True}


def test_unstreamed_trace_enqueues_events_payload(client, server):
    with client.trace(input="q") as t:
        assert t.stream is False
        t.add_event("tool", "search")
    (p,) = client._drain()
    assert [e["seq"] for e in p["events"]] == [0] and p["tool_calls"] == []
    assert server.requests == []


def test_legacy_trace_enqueue_unchanged(client, server):
    with client.trace(input="q", stream=True) as t:
        t.add_tool_call("search")
    (p,) = client._drain()
    assert set(p) == LEGACY_KEYS and p["tool_calls"][0]["name"] == "search"
    assert server.requests == []


def test_current_trace_contextvar():
    assert current_trace() is None
    with Trace(agent_id="a1") as t:
        assert current_trace() is t
        with Trace(agent_id="a1") as inner:
            assert current_trace() is inner
        assert current_trace() is t
    assert current_trace() is None

    async def run():
        async with Trace(agent_id="a1") as t:
            assert current_trace() is t
        assert current_trace() is None
    asyncio.run(run())
