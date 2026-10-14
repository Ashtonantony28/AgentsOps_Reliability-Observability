"""SDK request/response models against the backend OpenAPI schema, and the SDK end to end against the app."""
import dataclasses
import sys
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "sdk"))

from agentops_sdk import precheck as sdk  # noqa: E402
from agentops_sdk.async_client import AsyncAgentOpsClient  # noqa: E402
from agentops_sdk.trace import Trace as SdkTrace  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Span, Trace, TracePolicy  # noqa: E402
from app.schemas.policy import EventsAppendIn, PolicyCheckIn, PolicyCheckOut  # noqa: E402
from app.schemas.trace import EventIn, TraceIn  # noqa: E402

CONFIRM = 'policy confirm_before_delete severity high:\n  always tool("delete_*") -> once event("confirmation")'


def _schema(name):
    schemas = app.openapi()["components"]["schemas"]
    return schemas[name]


def _props(name):
    return set(_schema(name)["properties"])


def _event():
    return sdk.event_dict(0, "tool", "delete_user", "error", {"q": 1}, {"r": 2}, {"k": "v"}, latency_ms=5,
                          timestamp="2026-10-14T00:00:00+00:00")


def test_event_matches_event_in():
    assert set(_event()) == _props("EventIn")
    assert set(sdk.EVENT_KINDS) == set(_schema("EventIn")["properties"]["kind"]["enum"])
    assert EventIn.model_validate(_event()).model_dump(mode="json", exclude={"timestamp"}) == {
        k: v for k, v in _event().items() if k != "timestamp"}


def test_check_request_matches_policy_check_in():
    body = sdk.check_request("a1", "uid", _event())
    assert set(body) == _props("PolicyCheckIn")
    PolicyCheckIn.model_validate(body)
    PolicyCheckIn.model_validate(sdk.check_request("a1", None, _event()))


def test_events_request_matches_events_append_in():
    t = SdkTrace(agent_id="a1", environment="prod", input="hi", session_id="s1", metadata={"k": 1})
    t.add_event("tool", "search")
    t.mark_error("boom")
    first, last = t._batch(final=False)[2], t._batch(final=True)[2]
    assert set(last) | set(first) == _props("EventsAppendIn")
    assert EventsAppendIn.model_validate(first).input == "hi" and "output" not in first
    assert EventsAppendIn.model_validate(last).status == "error"
    t._sent = 1  # a later final push carries the outcome fields, not the starting ones
    assert t._batch(final=False) is None
    assert "input" not in t._batch(final=True)[2] and t._batch(final=True)[2]["environment"] == "prod"


def test_check_result_matches_policy_check_out():
    fields = {f.name for f in dataclasses.fields(sdk.CheckResult)} - {"error"}
    assert fields == _props("PolicyCheckOut")
    out = PolicyCheckOut(allowed=False, violations=[]).model_dump(mode="json")
    assert sdk.check_result(httpx.Response(200, json=out), fail_closed=False) == sdk.CheckResult(False, [])


def test_trace_payloads_validate():
    t = SdkTrace(agent_id="a1", input="hi")
    t.add_tool_call("search")
    assert TraceIn.model_validate(t.to_payload()).tool_calls[0].name == "search"
    t = SdkTrace(agent_id="a1", input="hi")
    t.add_event("tool", "search", q=1)
    t.add_event("confirmation", "user_confirmed")
    parsed = TraceIn.model_validate(t.to_payload())
    assert [(e.seq, e.kind, e.name, e.attrs) for e in parsed.events] == [
        (0, "tool", "search", {"q": 1}), (1, "confirmation", "user_confirmed", {})]


@pytest.fixture
def sdk_client(app_, api_key, agent, monkeypatch):
    real, transport = httpx.AsyncClient, httpx.ASGITransport(app=app_)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
    return AsyncAgentOpsClient(api_key=api_key, agent_id=agent.id, base_url="http://test")


async def test_sdk_end_to_end(db, org, sdk_client):
    db.add(TracePolicy(org_id=org.id, name="confirm_before_delete", source=CONFIRM, severity="high",
                       action="block"))
    await db.commit()
    blocked = await sdk_client.acheck("delete_user")
    assert not blocked.allowed and blocked.error is None
    assert [v["policy_name"] for v in blocked.violations] == ["confirm_before_delete"]
    assert "confirm_before_delete" in str(sdk.PolicyBlocked(blocked))

    async with sdk_client.trace(stream=True) as t:
        t.add_event("tool", "search")
        assert not (await t.acheck("delete_user")).allowed
        t.add_event("confirmation", "user_confirmed")
        assert (await t.acheck("delete_user")) == sdk.CheckResult(True, [])
        t.add_event("tool", "delete_user")
    trace = (await db.execute(select(Trace).execution_options(populate_existing=True))).scalar_one()
    spans = (await db.execute(select(Span).where(Span.trace_id == trace.id))).scalars().all()
    assert trace.trace_uid == t.trace_id and trace.meta == {"stream": "final"}
    assert sorted((s.seq, s.name) for s in spans) == [(0, "search"), (1, "user_confirmed"), (2, "delete_user")]


async def test_sdk_fails_open_when_server_errors(sdk_client):
    sdk_client.api_key = "wrong"
    result = await sdk_client.acheck("delete_user")
    assert result.allowed and result.error == "http 401"
    sdk_client.fail_closed = True
    assert not (await sdk_client.acheck("delete_user")).allowed
