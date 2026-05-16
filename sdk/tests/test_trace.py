from agentops_sdk.trace import Trace


def test_trace_payload_basics():
    t = Trace(agent_id="a1", input="hello")
    t.set_output("hi")
    t.add_tool_call(name="search", latency_ms=42, input={"q": "x"}, output={"hits": 1})
    p = t.to_payload()
    assert p["agent_id"] == "a1"
    assert p["input"] == "hello"
    assert p["output"] == "hi"
    assert p["tool_calls"][0]["name"] == "search"
    assert p["status"] == "success"


def test_trace_context_manager_error():
    t = Trace(agent_id="a1")
    try:
        with t:
            raise ValueError("boom")
    except ValueError:
        pass
    assert t.status == "error"
    assert "boom" in (t.error_message or "")
