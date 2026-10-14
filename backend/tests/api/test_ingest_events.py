import asyncio
import json

import pytest
from sqlalchemy import select
from starlette.testclient import TestClient

from app.models import AuditLog, PolicyRule, Span, Trace
from app.policy.events import Event, events_from_spans

TOOL_CALLS = [
    {"name": "search", "input": {"q": "x"}, "output": {"n": 1}, "latency_ms": 5},
    {"name": "fetch_url", "status": "error"},
    {"name": "send_email", "latency_ms": 9},
]
EVENTS = [
    {"seq": 0, "kind": "llm", "name": "plan", "attrs": {"model": "opus"}},
    {"seq": 1, "kind": "confirmation", "name": "user_confirmed"},
    {"seq": 2, "kind": "tool", "name": "delete_user", "status": "error", "attrs": {"target": "u1"}},
]


async def _ingest(client, headers, agent, **fields):
    return await client.post("/api/traces/ingest", headers=headers, json={"agent_id": agent.id, **fields})


async def _spans(db, trace_id):
    return (await db.execute(select(Span).where(Span.trace_id == trace_id))).scalars().all()


async def test_legacy_tool_calls_stored_with_seq(client, db, api_key_headers, auth_headers, agent):
    r = await _ingest(client, api_key_headers, agent, input="hi", tool_calls=TOOL_CALLS)
    assert r.status_code == 202, r.text
    tid = r.json()["id"]

    spans = await _spans(db, tid)
    assert sorted((s.seq, s.kind, s.name, s.status) for s in spans) == [
        (0, "tool", "search", "success"), (1, "tool", "fetch_url", "error"), (2, "tool", "send_email", "success"),
    ]
    assert all(s.attrs is None for s in spans)
    assert events_from_spans(spans) == [
        Event(0, "tool", "search", "success", {}),
        Event(1, "tool", "fetch_url", "error", {}),
        Event(2, "tool", "send_email", "success", {}),
    ]

    detail = (await client.get(f"/api/traces/{tid}", headers=auth_headers)).json()
    assert [(s["seq"], s["name"]) for s in detail["spans"]] == [(0, "search"), (1, "fetch_url"), (2, "send_email")]
    assert detail["spans"][0]["input"] == {"q": "x"} and detail["spans"][0]["latency_ms"] == 5


async def test_events_payload_stored(client, db, api_key_headers, auth_headers, agent):
    r = await _ingest(client, api_key_headers, agent, trace_id="uid-ev", events=EVENTS)
    assert r.status_code == 202, r.text
    assert r.json()["trace_uid"] == "uid-ev"
    tid = r.json()["id"]

    assert events_from_spans(await _spans(db, tid)) == [
        Event(0, "llm", "plan", "success", {"model": "opus"}),
        Event(1, "confirmation", "user_confirmed", "success", {}),
        Event(2, "tool", "delete_user", "error", {"target": "u1"}),
    ]
    detail = (await client.get(f"/api/traces/{tid}", headers=auth_headers)).json()
    assert [(s["seq"], s["kind"], s["name"], s["attrs"]) for s in detail["spans"]] == [
        (0, "llm", "plan", {"model": "opus"}),
        (1, "confirmation", "user_confirmed", None),
        (2, "tool", "delete_user", {"target": "u1"}),
    ]


async def test_detail_orders_by_seq_not_insert_time(client, db, api_key_headers, auth_headers, agent):
    events = [{**e, "timestamp": ts} for e, ts in
              zip(EVENTS, ["2026-01-03T00:00:00", "2026-01-02T00:00:00", "2026-01-01T00:00:00"])]
    tid = (await _ingest(client, api_key_headers, agent, events=events)).json()["id"]
    detail = (await client.get(f"/api/traces/{tid}", headers=auth_headers)).json()
    assert [s["seq"] for s in detail["spans"]] == [0, 1, 2]


@pytest.mark.parametrize("payload", [
    {"tool_calls": [{"name": "a"}], "events": [{"seq": 0, "name": "a"}]},
    {"events": [{"seq": 1, "name": "a"}]},
    {"events": [{"seq": 0, "name": "a"}, {"seq": 2, "name": "b"}]},
    {"events": [{"seq": 1, "name": "a"}, {"seq": 0, "name": "b"}]},
    {"events": [{"seq": 0, "name": "a"}, {"seq": 0, "name": "b"}]},
    {"events": [{"seq": 0, "name": "a", "kind": "bogus"}]},
    {"events": [{"seq": 0, "name": ""}]},
], ids=["both", "starts_at_1", "gap", "out_of_order", "dup", "unknown_kind", "empty_name"])
async def test_invalid_events_payload_422(client, db, api_key_headers, agent, payload):
    r = await _ingest(client, api_key_headers, agent, **payload)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "validation_error"
    assert (await db.execute(select(Trace))).scalars().all() == []


@pytest.mark.parametrize("payload", [
    {"tool_calls": [{"name": "search"}, {"name": "drop_db"}]},
    {"events": [{"seq": 0, "kind": "llm", "name": "plan"}, {"seq": 1, "kind": "tool", "name": "drop_db"}]},
], ids=["tool_calls", "events"])
async def test_forbid_tool_rule_blocks(client, db, org, api_key_headers, agent, payload):
    db.add(PolicyRule(org_id=org.id, name="no-drop", rule={"forbid_tool": "drop_db"}, action="block"))
    await db.commit()
    r = await _ingest(client, api_key_headers, agent, **payload)
    assert r.status_code == 403, r.text
    assert r.json()["error"]["details"]["rule"] == "no-drop"
    assert (await db.execute(select(Trace))).scalars().all() == []
    logs = (await db.execute(select(AuditLog).where(AuditLog.action == "policy_block"))).scalars().all()
    assert len(logs) == 1


async def test_ingest_publishes_to_redis(client, redis, org, api_key_headers, agent):
    ps = redis.pubsub()
    await ps.subscribe(f"traces:{org.id}")
    await ps.get_message(timeout=1)  # subscribe ack
    r = await _ingest(client, api_key_headers, agent, input="hello", tool_calls=TOOL_CALLS)
    msg = None
    for _ in range(20):
        msg = await ps.get_message(ignore_subscribe_messages=True, timeout=0.1)
        if msg:
            break
    await ps.aclose()
    assert msg and msg["channel"] == f"traces:{org.id}"
    data = json.loads(msg["data"])
    assert data["id"] == r.json()["id"] and data["agent_id"] == agent.id and data["input_preview"] == "hello"


async def test_bulk_ingest_accepts_both_shapes(client, db, api_key_headers, agent):
    r = await client.post("/api/traces/ingest/bulk", headers=api_key_headers, json={"traces": [
        {"agent_id": agent.id, "tool_calls": TOOL_CALLS[:1]},
        {"agent_id": agent.id, "events": EVENTS[:2]},
    ]})
    assert r.status_code == 202, r.text
    ids = [a["id"] for a in r.json()["accepted"]]
    assert len(ids) == 2
    assert [s.seq for s in await _spans(db, ids[1])] in ([0, 1], [1, 0])


def test_ws_ingest(app_, api_key, agent, session_maker):
    with TestClient(app_) as tc:
        with tc.websocket_connect(f"/ws/ingest?api_key={api_key}") as ws:
            ws.send_text(json.dumps({"type": "events", "trace_uid": "u1", "agent_id": agent.id,
                                     "events": [{"seq": 0, "name": "delete_user"}], "final": True}))
            streamed = ws.receive_json()
            assert streamed["ok"] is True and streamed["accepted"] == 1 and streamed["violations"] == []

            ws.send_text(json.dumps({"type": "events", "trace_uid": "u1", "agent_id": agent.id,
                                     "events": [{"seq": -1, "name": "x"}]}))
            bad = ws.receive_json()
            assert bad["ok"] is False and bad["error"].startswith("invalid_payload")

            ws.send_text(json.dumps({"agent_id": agent.id, "tool_calls": [{"name": "a"}, {"name": "b"}]}))
            ok = ws.receive_json()
            assert ok["ok"] is True

            ws.send_text(json.dumps({"agent_id": agent.id, "events": [{"seq": 0, "kind": "llm", "name": "p"}]}))
            assert ws.receive_json()["ok"] is True

            ws.send_text("not json")
            assert ws.receive_json()["error"].startswith("invalid_payload")

    async def spans():
        async with session_maker() as s:
            return (await s.execute(select(Span).where(Span.trace_id == ok["id"]))).scalars().all()
    rows = asyncio.run(spans())
    assert sorted((s.seq, s.kind, s.name) for s in rows) == [(0, "tool", "a"), (1, "tool", "b")]


def test_ws_ingest_rejects_bad_key(app_):
    from starlette.websockets import WebSocketDisconnect
    with TestClient(app_) as tc:
        with pytest.raises(WebSocketDisconnect) as exc:
            with tc.websocket_connect("/ws/ingest?api_key=nope") as ws:
                ws.receive_json()
        assert exc.value.code == 4401


async def test_legacy_empty_tool_name_still_ingests(client, db, api_key_headers, agent):
    r = await _ingest(client, api_key_headers, agent, tool_calls=[{"name": ""}])
    assert r.status_code == 202, r.text
    assert [(s.seq, s.name) for s in await _spans(db, r.json()["id"])] == [(0, "")]


async def test_aware_event_timestamp_stored_as_naive_utc(client, db, api_key_headers, agent):
    ev = {"seq": 0, "name": "search", "timestamp": "2026-10-14T10:00:00+02:00"}
    r = await _ingest(client, api_key_headers, agent, events=[ev])
    assert r.status_code == 202, r.text
    (span,) = await _spans(db, r.json()["id"])
    assert span.started_at.isoformat() == "2026-10-14T08:00:00"
