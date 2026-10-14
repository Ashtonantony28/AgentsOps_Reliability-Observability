import uuid

import pytest
from sqlalchemy import select

from app.middleware.auth import make_access_token
from app.models import Agent, AuditLog, Organization, OrgMembership, TracePolicy, User
from app.policy.dsl import PolicyParseError
from app.routers.trace_policies import parse_single

URL = "/api/trace-policies"
SRC_A = 'policy alpha severity high:\n  always not tool("rm")'
SRC_B = 'policy beta severity low:\n  always tool(*)'


@pytest.fixture
async def other_org(db):
    o = Organization(name="Other", slug=f"other-{uuid.uuid4().hex[:8]}")
    db.add(o)
    await db.flush()
    u = User(email=f"o-{uuid.uuid4().hex[:8]}@example.com", password_hash="x", full_name="o")
    db.add(u)
    await db.flush()
    db.add(OrgMembership(org_id=o.id, user_id=u.id, role="admin"))
    await db.commit()
    return o, {"Authorization": f"Bearer {make_access_token(u.id, o.id, 'admin')}"}


async def _create(client, headers, source=SRC_A, **kw):
    r = await client.post(URL, json={"source": source, **kw}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _err(r, status, code):
    assert r.status_code == status, r.text
    body = r.json()["error"]
    assert body["code"] == code
    return body


def test_parse_single():
    assert parse_single(SRC_A).name == "alpha"
    with pytest.raises(PolicyParseError, match="found 0"):
        parse_single("")
    with pytest.raises(PolicyParseError, match="found 2") as e:
        parse_single(SRC_A + "\n" + SRC_B)
    assert (e.value.line, e.value.col) == (1, 1)


async def test_create_list_roundtrip(client, auth_headers, org, agent):
    b = await _create(client, auth_headers, SRC_B, action="block", agent_id=agent.id, environment="prod")
    a = await _create(client, auth_headers, SRC_A, enabled=False)
    assert (a["name"], a["severity"], a["version"], a["action"], a["enabled"]) == ("alpha", "high", 1, "log", False)
    assert (b["name"], b["severity"], b["agent_id"], b["environment"]) == ("beta", "low", agent.id, "prod")
    assert a["org_id"] == org.id and a["source"] == SRC_A
    r = await client.get(URL, headers=auth_headers)
    assert r.status_code == 200
    assert [p["id"] for p in r.json()] == [a["id"], b["id"]]
    assert r.json()[1] == b


async def test_severity_defaults_to_medium(client, auth_headers):
    assert (await _create(client, auth_headers, "policy g:\n  always tool(*)"))["severity"] == "medium"


@pytest.mark.parametrize("source,line,col,msg", [
    ("policy p severity huge:\n  always tool(*)", 1, 19, "severity level"),
    ("", 1, 1, "expected exactly one policy block, found 0"),
    (SRC_A + "\n" + SRC_B, 1, 1, "expected exactly one policy block, found 2"),
])
async def test_validate_errors(client, viewer_headers, source, line, col, msg):
    r = await client.post(f"{URL}/validate", json={"source": source}, headers=viewer_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and len(body["errors"]) == 1
    err = body["errors"][0]
    assert (err["line"], err["col"]) == (line, col) and msg in err["message"]


async def test_validate_ok(client, viewer_headers):
    r = await client.post(f"{URL}/validate", json={"source": SRC_A}, headers=viewer_headers)
    assert r.json() == {"ok": True, "errors": []}


async def test_create_invalid_source_422(client, auth_headers):
    r = await client.post(URL, json={"source": "policy p:\n  always (tool(*)"}, headers=auth_headers)
    body = _err(r, 422, "invalid_policy")
    [e] = body["details"]["errors"]
    assert e["line"] == 2 and e["message"] == body["message"]
    r = await client.post(URL, json={"source": SRC_A + "\n" + SRC_B}, headers=auth_headers)
    assert "found 2" in _err(r, 422, "invalid_policy")["message"]
    assert (await client.get(URL, headers=auth_headers)).json() == []


async def test_oversized_policy_422(client, auth_headers):
    """A5-1: tags inline as shared nodes, so 750 bytes can expand to 2^30 leaves; create, update and validate refuse it."""
    src = ('tag t0 = tool("a")\n' + "".join(f"tag t{i} = @t{i - 1} and @t{i - 1}\n" for i in range(1, 31))
           + "policy p severity high:\n  always not @t30\n")
    assert "over 2000 atoms" in _err(await client.post(URL, json={"source": src}, headers=auth_headers),
                                     422, "invalid_policy")["message"]
    p = await _create(client, auth_headers)
    _err(await client.put(f"{URL}/{p['id']}", json={"source": src}, headers=auth_headers), 422, "invalid_policy")
    r = await client.post(f"{URL}/validate", json={"source": src}, headers=auth_headers)
    assert r.json()["ok"] is False


async def test_duplicate_name_409(client, auth_headers):
    await _create(client, auth_headers)
    r = await client.post(URL, json={"source": 'policy alpha:\n  always tool("x")'}, headers=auth_headers)
    _err(r, 409, "policy_name_taken")
    b = await _create(client, auth_headers, SRC_B)
    r = await client.put(f"{URL}/{b['id']}", json={"source": SRC_A}, headers=auth_headers)
    _err(r, 409, "policy_name_taken")
    r = await client.get(URL, headers=auth_headers)
    assert [p["name"] for p in r.json()] == ["alpha", "beta"]


async def test_same_name_allowed_across_orgs(client, auth_headers, other_org):
    await _create(client, auth_headers)
    await _create(client, other_org[1])


async def test_agent_from_other_org_404(client, auth_headers, db, other_org):
    foreign = Agent(org_id=other_org[0].id, name="foreign")
    db.add(foreign)
    await db.commit()
    r = await client.post(URL, json={"source": SRC_A, "agent_id": foreign.id}, headers=auth_headers)
    assert r.status_code == 404 and r.json()["error"]["message"] == "agent_not_found"
    p = await _create(client, auth_headers)
    r = await client.put(f"{URL}/{p['id']}", json={"agent_id": foreign.id}, headers=auth_headers)
    assert r.status_code == 404 and r.json()["error"]["message"] == "agent_not_found"


async def test_update_fields_without_source_keeps_version(client, auth_headers):
    p = await _create(client, auth_headers)
    r = await client.put(f"{URL}/{p['id']}", json={"action": "alert", "enabled": False, "environment": "staging"},
                         headers=auth_headers)
    assert r.status_code == 200, r.text
    u = r.json()
    assert (u["action"], u["enabled"], u["environment"], u["version"]) == ("alert", False, "staging", 1)
    assert u["updated_at"] >= p["updated_at"]


async def test_update_source_bumps_version(client, auth_headers):
    p = await _create(client, auth_headers)
    new = 'policy gamma severity critical:\n  always not tool("drop")'
    u = (await client.put(f"{URL}/{p['id']}", json={"source": new}, headers=auth_headers)).json()
    assert (u["name"], u["severity"], u["source"], u["version"]) == ("gamma", "critical", new, 2)
    same = (await client.put(f"{URL}/{p['id']}", json={"source": new, "action": "block"}, headers=auth_headers)).json()
    assert (same["version"], same["action"]) == (2, "block")
    r = await client.put(f"{URL}/{p['id']}", json={"source": "policy"}, headers=auth_headers)
    _err(r, 422, "invalid_policy")


async def test_update_source_same_name_new_severity(client, auth_headers):
    p = await _create(client, auth_headers)
    u = (await client.put(f"{URL}/{p['id']}", json={"source": SRC_A.replace("high", "low")},
                          headers=auth_headers)).json()
    assert (u["name"], u["severity"], u["version"]) == ("alpha", "low", 2)


async def test_explicit_null_clears_agent_and_env(client, auth_headers, agent):
    p = await _create(client, auth_headers, agent_id=agent.id, environment="prod")
    u = (await client.put(f"{URL}/{p['id']}", json={"enabled": True}, headers=auth_headers)).json()
    assert (u["agent_id"], u["environment"]) == (agent.id, "prod")
    u = (await client.put(f"{URL}/{p['id']}", json={"agent_id": None, "environment": None},
                          headers=auth_headers)).json()
    assert (u["agent_id"], u["environment"]) == (None, None)


async def test_viewer_cannot_write(client, auth_headers, viewer_headers):
    p = await _create(client, auth_headers)
    for method, path, body in [("POST", URL, {"source": SRC_B}), ("PUT", f"{URL}/{p['id']}", {"enabled": False}),
                               ("DELETE", f"{URL}/{p['id']}", None)]:
        r = await client.request(method, path, json=body, headers=viewer_headers)
        assert r.status_code == 403, r.text
    r = await client.get(URL, headers=viewer_headers)
    assert r.status_code == 200 and [x["id"] for x in r.json()] == [p["id"]]


async def test_cross_org_isolation(client, auth_headers, other_org):
    _, other_headers = other_org
    mine = await _create(client, auth_headers)
    theirs = await _create(client, other_headers, SRC_B)
    assert [x["id"] for x in (await client.get(URL, headers=auth_headers)).json()] == [mine["id"]]
    for method, body in [("PUT", {"enabled": False}), ("DELETE", None)]:
        r = await client.request(method, f"{URL}/{theirs['id']}", json=body, headers=auth_headers)
        assert r.status_code == 404 and r.json()["error"]["message"] == "policy_not_found"
    assert (await client.get(URL, headers=other_headers)).json()[0]["enabled"] is True


async def test_delete_archives(client, auth_headers, db):
    p = await _create(client, auth_headers)
    keep = await _create(client, auth_headers, SRC_B)
    r = await client.delete(f"{URL}/{p['id']}", headers=auth_headers)
    assert r.status_code == 200 and r.json() == {"ok": True}
    row = (await db.execute(select(TracePolicy).where(TracePolicy.id == p["id"]))).scalar_one()
    assert row.archived_at is not None and row.enabled
    assert (await client.delete(f"{URL}/{p['id']}", headers=auth_headers)).status_code == 404
    assert (await client.put(f"{URL}/{p['id']}", json={}, headers=auth_headers)).status_code == 404
    assert [x["id"] for x in (await client.get(URL, headers=auth_headers)).json()] == [keep["id"]]

    again = await _create(client, auth_headers)  # an archived policy's name is free again
    _err(await client.post(URL, json={"source": SRC_A}, headers=auth_headers), 409, "policy_name_taken")
    listed = (await client.get(URL, params={"include_archived": True}, headers=auth_headers)).json()
    assert [(x["id"], x["archived_at"] is None) for x in listed] == [
        (again["id"], True), (p["id"], False), (keep["id"], True)]
    assert again["archived_at"] is None


async def test_archived_name_does_not_block_rename(client, auth_headers):
    p = await _create(client, auth_headers)
    b = await _create(client, auth_headers, SRC_B)
    await client.delete(f"{URL}/{p['id']}", headers=auth_headers)
    r = await client.put(f"{URL}/{b['id']}", json={"source": SRC_B.replace("beta", "alpha")}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["name"] == "alpha"


async def test_audit_log_rows(client, auth_headers, db, admin_user):
    p = await _create(client, auth_headers, action="alert")
    await client.put(f"{URL}/{p['id']}", json={"source": SRC_A.replace("alpha", "renamed"), "enabled": False},
                     headers=auth_headers)
    await client.delete(f"{URL}/{p['id']}", headers=auth_headers)
    rows = (await db.execute(
        select(AuditLog).where(AuditLog.resource_type == "trace_policy").order_by(AuditLog.created_at)
    )).scalars().all()
    assert [r.action for r in rows] == ["trace_policy_create", "trace_policy_update", "trace_policy_delete"]
    assert all(r.resource_id == p["id"] and r.actor_type == "user" and r.actor_id == admin_user.id for r in rows)
    assert rows[0].details == {"name": "alpha", "action": "alert"}
    assert rows[1].details["name"] == "renamed" and rows[1].details["version"] == 2
    assert rows[1].details["enabled"] is False and "action" not in rows[1].details
