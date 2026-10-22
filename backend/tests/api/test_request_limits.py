"""AUDIT-13 A13-2: request bodies over MAX_REQUEST_BYTES are a 413 before they are read, API-key routes authenticate
before parsing their bodies (and parse them off the event loop), and password hashing runs off the loop."""
import json
import sys
import threading
from pathlib import Path

import httpx
import pytest

import app.middleware.auth as auth_mw
import app.middleware.body_limit as body_limit
from app.schemas.trace import MAX_REQUEST_BYTES
from app.services import auth_service
from tests.audit.test_audit_13 import _max_stall

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "sdk"))
from agentops_sdk import precheck as sdk  # noqa: E402

EVENT = {"seq": 0, "name": "search"}
API_KEY_ROUTES = [
    ("/api/trace-policies/check", lambda a: {"agent_id": a, "candidate": EVENT}),
    ("/api/traces/ingest", lambda a: {"agent_id": a, "events": [EVENT]}),
    ("/api/traces/ingest/bulk", lambda a: {"traces": [{"agent_id": a, "events": [EVENT]}]}),
    ("/api/traces/u1/events", lambda a: {"agent_id": a, "events": [EVENT], "final": False}),
]
PATHS = [p for p, _ in API_KEY_ROUTES]


def _padded(body: dict, size: int) -> bytes:
    """`body` as JSON of exactly `size` bytes."""
    raw = json.dumps({**body, "pad": ""})
    return json.dumps({**body, "pad": "x" * (size - len(raw))}).encode()


def _too_large(r: httpx.Response, limit: int) -> None:
    assert r.status_code == 413, r.text
    err = r.json()["error"]
    assert err["code"] == "request_too_large" and err["details"]["max_bytes"] == limit, err


@pytest.fixture
def small_limit(monkeypatch):
    monkeypatch.setattr(body_limit, "MAX_REQUEST_BYTES", 4_096)
    return 4_096


@pytest.fixture
def parsed(monkeypatch):
    """The threads API-key route bodies were parsed in."""
    threads, real = [], auth_mw._validated
    monkeypatch.setattr(auth_mw, "_validated", lambda m, b: threads.append(threading.current_thread()) or real(m, b))
    return threads


async def _asgi(app_, path: str, headers: dict, chunks: list[bytes]) -> tuple[int, int]:
    """Calls the app directly; returns the response status and how many body chunks it read."""
    read, sent = [0], {}

    async def receive():
        read[0] += 1
        i = read[0] - 1
        return {"type": "http.request", "body": chunks[i] if i < len(chunks) else b"", "more_body": i + 1 < len(chunks)}

    async def send(message):
        sent.setdefault("status", message.get("status"))

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "", "server": ("test", 80),
             "client": ("127.0.0.1", 1), "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}
    await app_(scope, receive, send)
    return sent["status"], read[0]


@pytest.mark.parametrize("path", [*PATHS, "/api/auth/login"])
async def test_a_declared_length_over_the_limit_is_refused_unread(app_, api_key_headers, path):
    headers = {**api_key_headers, "content-type": "application/json", "content-length": str(MAX_REQUEST_BYTES + 1)}
    assert await _asgi(app_, path, headers, [b"{}"]) == (413, 0)


async def test_the_limit_is_the_largest_body_taken(client, api_key_headers, agent, small_limit):
    for path, body in API_KEY_ROUTES:
        at = _padded(body(agent.id), small_limit)
        r = await client.post(path, content=at, headers={**api_key_headers, "content-type": "application/json"})
        assert r.status_code in (200, 202), (path, r.text)
        r = await client.post(path, content=at + b" ", headers={**api_key_headers, "content-type": "application/json"})
        _too_large(r, small_limit)


async def test_a_bad_key_with_a_body_over_the_limit_is_a_413(client, small_limit):
    r = await client.post(PATHS[0], content=b"x" * (small_limit + 1), headers={"X-API-Key": "nope"})
    _too_large(r, small_limit)


@pytest.mark.parametrize("path", [PATHS[0], PATHS[3], "/api/auth/login"])
async def test_an_unsized_body_is_read_only_to_the_limit(app_, api_key_headers, small_limit, path):
    chunks = [b"x" * 1_000] * 50
    status, read = await _asgi(app_, path, {**api_key_headers, "content-type": "application/json"}, chunks)
    assert status == 413 and read == small_limit // 1_000 + 1, (status, read)


async def test_an_unsized_body_over_the_limit_through_the_client(client, api_key_headers, small_limit):
    pulled = []

    async def chunks():
        for i in range(50):
            pulled.append(i)
            yield b" " * 1_000

    r = await client.post(PATHS[0], content=chunks(), headers={**api_key_headers, "content-type": "application/json"})
    _too_large(r, small_limit)
    assert len(pulled) <= small_limit // 1_000 + 2, len(pulled)


@pytest.mark.parametrize("body", [b"{not json", b'{"agent_id": 1}', b"[]", b""])
@pytest.mark.parametrize("path", PATHS)
async def test_api_key_routes_authenticate_before_parsing(client, parsed, path, body):
    for headers in ({}, {"X-API-Key": "nope"}):
        r = await client.post(path, content=body, headers={**headers, "content-type": "application/json"})
        assert r.status_code == 401, r.text
    assert parsed == []


async def test_an_unauthenticated_body_at_the_limit_is_not_parsed(client, parsed, monkeypatch):
    monkeypatch.setattr(body_limit, "MAX_REQUEST_BYTES", 2_000_000)
    body = json.dumps({"agent_id": "x", "candidate": {**EVENT, "attrs": {f"k{j}": [j] for j in range(90_000)}}})
    assert len(body) < 2_000_000
    send = client.post(PATHS[0], content=body, headers={"content-type": "application/json", "X-API-Key": "nope"})
    r, stall = await _max_stall(send)
    assert r.status_code == 401 and parsed == []
    assert stall < 0.1, stall


@pytest.mark.parametrize("path,body", API_KEY_ROUTES)
async def test_authenticated_bodies_are_parsed_off_the_loop(client, api_key_headers, agent, parsed, path, body):
    r = await client.post(path, json=body(agent.id), headers=api_key_headers)
    assert r.status_code in (200, 202), r.text
    assert len(parsed) == 1 and parsed[0] is not threading.main_thread()


@pytest.mark.parametrize("path", PATHS)
async def test_authenticated_bad_bodies_are_422s_as_before(client, api_key_headers, path):
    r = await client.post(path, content=b"{not json", headers={**api_key_headers, "content-type": "application/json"})
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "validation_error" and err["details"][0]["type"] == "json_invalid"
    r = await client.post(path, json={"agent_id": "a", "events": [{"seq": -1, "name": "x"}], "traces": 1,
                                      "candidate": {"seq": -1, "name": "x"}}, headers=api_key_headers)
    assert r.status_code == 422, r.text
    assert all(d["loc"][0] == "body" for d in r.json()["error"]["details"])
    r = await client.post(path, content=b"", headers={**api_key_headers, "content-type": "application/json"})
    assert r.status_code == 422 and r.json()["error"]["details"][0]["type"] == "missing", r.text


async def test_the_key_is_still_marked_used(client, db, api_key_headers, agent):
    from sqlalchemy import select

    from app.models import ApiKey
    r = await client.post(PATHS[0], json={"agent_id": agent.id, "candidate": EVENT}, headers=api_key_headers)
    assert r.status_code == 200, r.text
    db.expire_all()
    assert (await db.execute(select(ApiKey.last_used_at))).scalar_one() is not None


async def test_the_sdk_follows_fail_closed_on_a_body_limit_413(client, api_key_headers, agent, small_limit):
    big = {"q": "x" * small_limit}
    body = sdk.check_request(agent.id, None, sdk.event_dict(0, "tool", "search", input=big))
    r = await client.post(PATHS[0], json=body, headers=api_key_headers)
    _too_large(r, small_limit)
    assert sdk.check_result(r, fail_closed=False) == sdk.CheckResult(True, [], "http 413")
    assert sdk.check_result(r, fail_closed=True) == sdk.CheckResult(False, [], "http 413")
    r = await client.post(PATHS[3], json=sdk.events_request(agent.id, [sdk.event_dict(0, "tool", "s", input=big)],
                                                            False), headers=api_key_headers)
    _too_large(r, small_limit)
    assert sdk.push_outcome(r) == ("max_bytes", small_limit)  # SG-A14.1: split, not stopped


@pytest.fixture
def hashed_in(monkeypatch):
    """The threads bcrypt ran in."""
    threads = []
    for name in ("hash_password", "verify_password"):
        real = getattr(auth_service, name)
        monkeypatch.setattr(auth_service, name,
                            lambda *a, real=real: threads.append(threading.current_thread()) or real(*a))
    return threads


async def test_register_and_login_hash_off_the_loop(client, hashed_in):
    creds = {"email": "off-loop@example.com", "password": "password123"}
    r, stall = await _max_stall(client.post("/api/auth/register", json=creds))
    assert r.status_code == 200 and r.json()["access_token"], r.text
    assert stall < 0.1, stall
    r, stall = await _max_stall(client.post("/api/auth/login", json=creds))
    assert r.status_code == 200 and r.json()["access_token"], r.text
    assert stall < 0.1, stall
    assert len(hashed_in) == 3 and threading.main_thread() not in hashed_in


async def test_login_and_register_outcomes(client):
    creds = {"email": "outcomes@example.com", "password": "password123"}
    assert (await client.post("/api/auth/register", json=creds)).status_code == 200
    r = await client.post("/api/auth/register", json=creds)
    assert r.status_code == 400 and r.json()["error"]["message"] == "email_exists"
    for body in ({**creds, "password": "wrong-password"}, {**creds, "email": "nobody@example.com"}):
        r = await client.post("/api/auth/login", json=body)
        assert r.status_code == 401 and r.json()["error"]["message"] == "invalid_credentials"
    r = await client.post("/api/auth/login", json=creds)
    assert r.status_code == 200
    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"})
    assert me.status_code == 200 and me.json()["user"]["email"] == creds["email"]
