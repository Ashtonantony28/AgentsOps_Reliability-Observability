from datetime import datetime

import pytest
from pydantic import ValidationError

from app.policy import ast
from app.policy.checker import Monitor, Violation, check_trace, compile_policy, would_violate
from app.policy.counterexample import minimal_counterexample
from app.policy.dsl import PolicyParseError, parse
from app.policy.events import Event, events_from_spans, events_from_trace
from app.policy.reference import evaluate
from app.schemas.policy import (
    EventsAppendIn,
    PolicyCheckIn,
    TracePolicyCreate,
    TracePolicyOut,
    ViolationMessage,
    ViolationOut,
    WsEventsMessage,
)
from app.schemas.trace import EventIn, TraceIn


def test_contract_symbols_import():
    for name in ["Match", "Not", "And", "Or", "Implies", "Once", "Historically", "Previously", "Since", "Count", "Policy"]:
        assert isinstance(getattr(ast, name), type)
    assert issubclass(PolicyParseError, ValueError)
    for fn in [events_from_trace, events_from_spans, parse, check_trace, compile_policy,
               would_violate, minimal_counterexample, evaluate]:
        assert callable(fn)
    assert {"policy_id", "policy_name", "severity", "at_seq", "counterexample", "message", "minimal"} == set(
        Violation.__dataclass_fields__)


def test_ast_nodes_are_frozen_and_hashable():
    m = ast.Match(kind="tool", globs=("a*",), status="error", attrs=(("k", 1),))
    assert hash(m) == hash(ast.Match(kind="tool", globs=("a*",), status="error", attrs=(("k", 1),)))
    with pytest.raises(Exception):
        m.kind = "llm"
    c = ast.Count(pred=m, op="<=", bound=3, by="name")
    assert c.by == "name"
    assert ast.Policy("p", "low", m, source="x") == ast.Policy("p", "low", m, source="y")


def test_parse_error_fields():
    e = PolicyParseError(3, 7, "expected ':'")
    assert (e.line, e.col, e.message) == (3, 7, "expected ':'")
    assert str(e) == "3:7: expected ':'"


def _roundtrip(model):
    return type(model).model_validate_json(model.model_dump_json())


def test_event_in_roundtrip():
    e = EventIn(seq=4, kind="confirmation", name="user_confirmed", status="error",
                input={"q": [1, 2]}, output={"ok": True}, attrs={"secret": True, "n": 1.5},
                latency_ms=12, timestamp=datetime(2026, 10, 13, 12, 0, 0))
    assert _roundtrip(e) == e
    d = EventIn(seq=0, name="x")
    assert (d.kind, d.status, d.input, d.attrs, d.timestamp) == ("tool", "success", {}, {}, None)
    assert _roundtrip(d) == d


def test_violation_out_roundtrip():
    v = ViolationOut(id="v1", trace_id="t1", agent_id="a1", policy_id="p1", policy_name="no_exfil",
                     policy_version=2, severity="critical", at_seq=7, counterexample=[1, 3, 7],
                     counterexample_status="minimal", message="exfil after secret", mode="live", detected_at=datetime(2026, 10, 13))
    assert _roundtrip(v) == v
    pre = ViolationOut(agent_id="a1", policy_id="p1", policy_name="x", policy_version=1, severity="low",
                       at_seq=0, counterexample=[], counterexample_status="none", message="", mode="precheck", detected_at=datetime(2026, 1, 1))
    assert pre.id is None and pre.trace_id is None
    assert _roundtrip(pre) == pre
    msg = ViolationMessage(data=v)
    assert msg.model_dump(mode="json")["type"] == "violation"
    assert _roundtrip(msg) == msg


def test_trace_policy_out_roundtrip():
    p = TracePolicyOut(id="p1", org_id="o1", name="retry_cap", source="policy retry_cap severity medium: ...",
                       severity="medium", action="block", agent_id=None, environment="prod", enabled=True,
                       version=3, created_at=datetime(2026, 10, 1), updated_at=datetime(2026, 10, 2))
    assert _roundtrip(p) == p


def test_trace_policy_out_from_orm():
    from app.models import TracePolicy
    row = TracePolicy(id="p1", org_id="o1", name="n", source="s", severity="high", action="alert",
                      agent_id=None, environment=None, enabled=False, version=1,
                      created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1))
    out = TracePolicyOut.model_validate(row)
    assert (out.name, out.severity, out.action, out.enabled) == ("n", "high", "alert", False)


@pytest.mark.parametrize("bad", [
    {"seq": -1, "name": "x"},
    {"seq": 0, "name": "x", "kind": "telepathy"},
    {"seq": 0, "name": ""},
    {"seq": 0},
    {"seq": "a", "name": "x"},
], ids=["negative_seq", "unknown_kind", "empty_name", "missing_name", "non_int_seq"])
def test_event_in_rejects(bad):
    with pytest.raises(ValidationError):
        EventIn.model_validate(bad)


@pytest.mark.parametrize("bad", [
    {"severity": "urgent"}, {"mode": "offline"}, {"counterexample": "1,2"},
], ids=["severity", "mode", "counterexample"])
def test_violation_out_rejects(bad):
    base = dict(agent_id="a", policy_id="p", policy_name="n", policy_version=1, severity="low", at_seq=0,
                counterexample=[0], message="", mode="batch", detected_at=datetime(2026, 1, 1))
    with pytest.raises(ValidationError):
        ViolationOut.model_validate({**base, **bad})


def test_trace_policy_create_rejects_unknown_action():
    assert TracePolicyCreate(source="x").action == "log"
    with pytest.raises(ValidationError):
        TracePolicyCreate(source="x", action="quarantine")


def test_trace_in_rejects_events_and_tool_calls():
    with pytest.raises(ValidationError, match="not both"):
        TraceIn(agent_id="a", tool_calls=[{"name": "t"}], events=[{"seq": 0, "name": "t"}])


@pytest.mark.parametrize("seqs", [[1], [0, 2], [1, 0], [0, 0], [0, 1, 3]])
def test_trace_in_rejects_bad_seq_order(seqs):
    with pytest.raises(ValidationError, match="seq"):
        TraceIn(agent_id="a", events=[{"seq": s, "name": f"e{s}"} for s in seqs])


def test_trace_in_accepts_contiguous_events():
    t = TraceIn(agent_id="a", events=[{"seq": i, "name": f"e{i}"} for i in range(3)])
    assert [e.seq for e in t.events] == [0, 1, 2]


def test_ws_events_message_and_check_in():
    m = WsEventsMessage.model_validate({"type": "events", "trace_uid": "t", "agent_id": "a",
                                        "events": [{"seq": 0, "name": "x"}]})
    assert m.final is False and isinstance(m, EventsAppendIn)
    with pytest.raises(ValidationError):
        WsEventsMessage.model_validate({"type": "trace", "trace_uid": "t", "agent_id": "a", "events": []})
    with pytest.raises(ValidationError):
        WsEventsMessage.model_validate({"type": "events", "agent_id": "a", "events": []})
    c = PolicyCheckIn(agent_id="a", candidate={"seq": 5, "name": "send_email"})
    assert c.trace_uid is None and c.candidate.kind == "tool"


def test_events_from_trace_legacy_tool_calls():
    t = TraceIn(agent_id="a", tool_calls=[{"name": "search", "status": "error"}, {"name": "delete_user"}])
    assert events_from_trace(t) == [
        Event(0, "tool", "search", "error", {}),
        Event(1, "tool", "delete_user", "success", {}),
    ]


def test_events_from_trace_events():
    t = TraceIn(agent_id="a", events=[
        {"seq": 0, "kind": "confirmation", "name": "user_confirmed", "attrs": {"by": "u1"}, "input": {"big": "x"}},
        {"seq": 1, "name": "delete_user", "status": "timeout"},
    ])
    assert events_from_trace(t) == [
        Event(0, "confirmation", "user_confirmed", "success", {"by": "u1"}),
        Event(1, "tool", "delete_user", "timeout", {}),
    ]
    assert events_from_trace(TraceIn(agent_id="a")) == []


def test_events_from_spans_orders_by_seq():
    from app.models import Span
    spans = [
        Span(seq=2, kind="tool", name="c", status="success", attrs=None),
        Span(seq=0, kind="llm", name="a", status="success", attrs={"m": "opus"}),
        Span(seq=1, kind="tool", name="b", status="error", attrs={}),
    ]
    assert events_from_spans(spans) == [
        Event(0, "llm", "a", "success", {"m": "opus"}),
        Event(1, "tool", "b", "error", {}),
        Event(2, "tool", "c", "success", {}),
    ]
