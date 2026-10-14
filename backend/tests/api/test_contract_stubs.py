import pytest

from app.main import app

SOURCE = "policy p severity low:\n  always tool(*)"
EVENT = {"seq": 0, "kind": "tool", "name": "delete_user"}

# (method, path, auth, body, admin_only)
JWT_ROUTES = [
    ("GET", "/api/trace-policies", None, False),
    ("POST", "/api/trace-policies", {"source": SOURCE}, True),
    ("PUT", "/api/trace-policies/p1", {"source": SOURCE}, True),
    ("DELETE", "/api/trace-policies/p1", None, True),
    ("POST", "/api/trace-policies/validate", {"source": SOURCE}, False),
    ("GET", "/api/traces/t1/violations", None, False),
    ("GET", "/api/reports/violations.csv", None, False),
]
API_KEY_ROUTES = [
    ("POST", "/api/trace-policies/check", {"agent_id": "a1", "candidate": EVENT}),
    ("POST", "/api/traces/uid-1/events", {"agent_id": "a1", "events": [EVENT], "final": True}),
]
ALL = [r[:3] for r in JWT_ROUTES] + API_KEY_ROUTES
IMPLEMENTED = {"/api/trace-policies", "/api/trace-policies/check", "/api/trace-policies/p1", "/api/trace-policies/validate",
               "/api/traces/uid-1/events", "/api/traces/t1/violations", "/api/reports/violations.csv"}
JWT_STUBS = [r for r in JWT_ROUTES if r[1] not in IMPLEMENTED]
API_KEY_STUBS = [r for r in API_KEY_ROUTES if r[1] not in IMPLEMENTED]
_ids = lambda routes: [f"{r[0]} {r[1]}" for r in routes]  # noqa: E731


async def _call(client, method, path, body, headers=None):
    return await client.request(method, path, json=body, headers=headers or {})


def _assert_501(r):
    assert r.status_code == 501, r.text
    assert r.json()["error"]["message"] == "not_implemented"


@pytest.mark.parametrize("method,path,body", ALL, ids=_ids(ALL))
async def test_requires_auth(client, method, path, body):
    r = await _call(client, method, path, body)
    assert r.status_code == 401, r.text


@pytest.mark.parametrize("method,path,body,admin_only", JWT_STUBS, ids=_ids(JWT_STUBS))
async def test_jwt_routes_501_for_admin(client, auth_headers, method, path, body, admin_only):
    _assert_501(await _call(client, method, path, body, auth_headers))


@pytest.mark.parametrize("method,path,body,admin_only", JWT_ROUTES, ids=_ids(JWT_ROUTES))
async def test_jwt_routes_viewer(client, viewer_headers, method, path, body, admin_only):
    r = await _call(client, method, path, body, viewer_headers)
    if admin_only:
        assert r.status_code == 403, r.text
        assert r.json()["error"]["message"] == "insufficient_role"
    else:
        assert r.status_code != 403, r.text


@pytest.mark.parametrize("method,path,body", API_KEY_STUBS, ids=_ids(API_KEY_STUBS))
async def test_api_key_routes_501(client, api_key_headers, method, path, body):
    _assert_501(await _call(client, method, path, body, api_key_headers))


@pytest.mark.parametrize("method,path,body", API_KEY_ROUTES, ids=_ids(API_KEY_ROUTES))
async def test_api_key_routes_reject_bad_key_and_jwt(client, auth_headers, method, path, body):
    assert (await _call(client, method, path, body, {"X-API-Key": "nope"})).status_code == 401
    assert (await _call(client, method, path, body, auth_headers)).status_code == 401


@pytest.mark.parametrize("method,path,body,admin_only", JWT_ROUTES, ids=_ids(JWT_ROUTES))
async def test_jwt_routes_reject_api_key(client, api_key_headers, method, path, body, admin_only):
    assert (await _call(client, method, path, body, api_key_headers)).status_code == 401


async def test_api_key_routes_validate_body(client, api_key_headers):
    r = await client.post("/api/traces/uid-1/events", headers=api_key_headers,
                          json={"agent_id": "a1", "events": [{"seq": -1, "name": "x"}]})
    assert r.status_code == 422
    r = await client.post("/api/trace-policies/check", headers=api_key_headers, json={"agent_id": "a1"})
    assert r.status_code == 422


async def test_existing_trace_detail_route_not_shadowed(client, auth_headers):
    r = await client.get("/api/traces/missing-id", headers=auth_headers)
    assert r.status_code == 404


def test_routes_in_openapi():
    spec = app.openapi()
    paths = spec["paths"]
    expected = {
        "/api/trace-policies": {"get", "post"},
        "/api/trace-policies/{policy_id}": {"put", "delete"},
        "/api/trace-policies/validate": {"post"},
        "/api/trace-policies/check": {"post"},
        "/api/traces/{trace_uid}/events": {"post"},
        "/api/traces/{trace_id}/violations": {"get"},
        "/api/reports/violations.csv": {"get"},
    }
    for path, methods in expected.items():
        assert path in paths, path
        assert methods <= set(paths[path]), (path, set(paths[path]))
    schemas = spec["components"]["schemas"]
    for name in ["TracePolicyCreate", "TracePolicyUpdate", "TracePolicyOut", "PolicyValidateIn",
                 "PolicyValidateOut", "PolicyCheckIn", "PolicyCheckOut", "EventsAppendIn",
                 "EventsAppendOut", "ViolationOut", "EventIn"]:
        assert name in schemas, name
    ref = lambda path, method: paths[path][method]["requestBody"]["content"]["application/json"]["schema"]["$ref"]  # noqa: E731
    assert ref("/api/trace-policies", "post").endswith("/TracePolicyCreate")
    assert ref("/api/trace-policies/{policy_id}", "put").endswith("/TracePolicyUpdate")
    assert ref("/api/trace-policies/check", "post").endswith("/PolicyCheckIn")
    assert ref("/api/traces/{trace_uid}/events", "post").endswith("/EventsAppendIn")
