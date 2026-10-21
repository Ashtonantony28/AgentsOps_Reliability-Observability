"""SG-P0 platform contracts: invite tokens, password change, webhook signing secrets, Slack OAuth state, eval runner."""
import uuid
from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa
from pydantic import TypeAdapter, ValidationError

from app.main import app
from app.middleware.auth import make_access_token, make_refresh_token
from app.models import EvaluationSet, Integration, Invite, OAuthState, Organization
from app.schemas.auth import AcceptInviteRequest, InviteOut, MemberOut, PasswordChangeRequest
from app.schemas.evaluation import (
    EvalRunner,
    EvalSetCreate,
    EvalSetOut,
    EvalSetUpdate,
    TracesRunner,
    WebhookRunner,
)
from app.schemas.integration import (
    IntegrationCreatedOut,
    IntegrationOut,
    SigningSecretOut,
    SlackOAuthCallbackOut,
    SlackOAuthStartOut,
)
from tests.conftest import _member

ACCEPT = {"token": "t" * 43, "password": "Secret1234!"}
PASSWORD = {"current_password": "old-password", "new_password": "New-password1"}
runner_adapter = TypeAdapter(EvalRunner)


def _err(r):
    return r.json()["error"]["message"]


@pytest.fixture
async def developer_headers(db, org):
    u = await _member(db, org, "developer")
    return {"Authorization": f"Bearer {make_access_token(u.id, org.id, 'developer')}"}


@pytest.fixture
async def outsider_headers(db, admin_user):
    """An admin's token for an org they are not a member of."""
    other = Organization(name="Other", slug=f"other-{uuid.uuid4().hex[:8]}")
    db.add(other)
    await db.commit()
    return {"Authorization": f"Bearer {make_access_token(admin_user.id, other.id, 'admin')}"}


@pytest.fixture
async def integration(db, org):
    i = Integration(org_id=org.id, kind="webhook", name="Hook", config={"url": "https://example.com/h"})
    db.add(i)
    await db.commit()
    return i


# --- stub routes: 501 behind their auth ---

async def test_accept_invite_is_a_stub_behind_its_token(client):
    # the token in the body is the only credential; a valid body reaches the stub
    r = await client.post("/api/auth/accept-invite", json=ACCEPT)
    assert r.status_code == 501 and _err(r) == "not_implemented"


@pytest.mark.parametrize("body", [
    {"password": "Secret1234!"},
    {"token": "", "password": "Secret1234!"},
    {"token": "t" * 257, "password": "Secret1234!"},
    {"token": "t", "password": "short"},
    {"token": "t", "password": "p" * 129},
])
async def test_accept_invite_validates_its_body(client, body):
    assert (await client.post("/api/auth/accept-invite", json=body)).status_code == 422


async def test_password_change_requires_a_signed_in_user(client, org, admin_user, api_key_headers, outsider_headers):
    assert _err(await client.post("/api/auth/password", json=PASSWORD)) == "missing_auth"
    r = await client.post("/api/auth/password", json=PASSWORD, headers=api_key_headers)
    assert r.status_code == 401
    refresh, _ = make_refresh_token(admin_user.id)
    r = await client.post("/api/auth/password", json=PASSWORD, headers={"Authorization": f"Bearer {refresh}"})
    assert r.status_code == 401 and _err(r) == "invalid_token_type"
    r = await client.post("/api/auth/password", json=PASSWORD, headers=outsider_headers)
    assert r.status_code == 403 and _err(r) == "org_membership_required"
    # auth is answered before the body
    assert (await client.post("/api/auth/password", json={})).status_code == 401


@pytest.mark.parametrize("role", ["viewer", "developer", "admin", "owner"])
async def test_password_change_is_a_stub_for_any_member(client, db, org, role):
    u = await _member(db, org, role)
    headers = {"Authorization": f"Bearer {make_access_token(u.id, org.id, role)}"}
    r = await client.post("/api/auth/password", json=PASSWORD, headers=headers)
    assert r.status_code == 501 and _err(r) == "not_implemented"


@pytest.mark.parametrize("body", [
    {"new_password": "New-password1"},
    {"current_password": "x", "new_password": "short"},
    {"current_password": "x", "new_password": "p" * 129},
    {"current_password": "x" * 129, "new_password": "New-password1"},
])
async def test_password_change_validates_its_body(client, auth_headers, body):
    assert (await client.post("/api/auth/password", json=body, headers=auth_headers)).status_code == 422


async def test_signing_secret_rotation_is_an_admin_stub(client, integration, viewer_headers, developer_headers,
                                                        auth_headers, api_key_headers, outsider_headers):
    path = f"/api/integrations/{integration.id}/signing-secret"
    assert (await client.post(path)).status_code == 401
    assert (await client.post(path, headers=api_key_headers)).status_code == 401
    assert _err(await client.post(path, headers=outsider_headers)) == "org_membership_required"
    for headers in (viewer_headers, developer_headers):
        r = await client.post(path, headers=headers)
        assert r.status_code == 403 and _err(r) == "insufficient_role"
    r = await client.post(path, headers=auth_headers)
    assert r.status_code == 501 and _err(r) == "not_implemented"


# --- existing routes keep their behaviour; new response shapes frozen ---

async def test_invite_route_returns_no_password(client, auth_headers):
    r = await client.post("/api/auth/invite", json={"email": "new@example.com", "role": "viewer"},
                          headers=auth_headers)
    assert r.status_code == 200
    assert set(r.json()) == set(MemberOut.model_fields)
    assert not any("password" in k or "token" in k for k in r.json())


def test_invite_out_carries_no_secret():
    assert not any("password" in f or "token" in f for f in InviteOut.model_fields)


async def test_created_integration_has_a_signing_secret_slot_and_lists_never_do(client, auth_headers):
    r = await client.post("/api/integrations", json={"kind": "webhook", "name": "H", "config": {}},
                          headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["signing_secret"] is None  # generated from SG-P1 on
    listed = (await client.get("/api/integrations", headers=auth_headers)).json()
    assert listed and all("signing_secret" not in i for i in listed)


async def test_listed_integration_never_shows_a_stored_secret(client, db, integration, auth_headers):
    integration.signing_secret = "s" * 64
    await db.commit()
    listed = (await client.get("/api/integrations", headers=auth_headers)).json()
    assert "s" * 64 not in str(listed)
    r = await client.patch(f"/api/integrations/{integration.id}", json={"name": "N"}, headers=auth_headers)
    assert "signing_secret" not in r.json()


# --- schema round trips ---

@pytest.mark.parametrize("model,data", [
    (InviteOut, {"email": "a@b.co", "role": "viewer", "status": "pending", "invite_id": "i1",
                 "expires_at": "2026-10-24T00:00:00"}),
    (InviteOut, {"email": "a@b.co", "role": "admin", "status": "added", "invite_id": None, "expires_at": None}),
    (AcceptInviteRequest, ACCEPT),
    (PasswordChangeRequest, PASSWORD),
    (SigningSecretOut, {"signing_secret": "ab" * 32}),
    (SlackOAuthStartOut, {"url": "https://slack.com/oauth/v2/authorize?state=x"}),
    (SlackOAuthCallbackOut, {"ok": True, "team": "T"}),
    (WebhookRunner, {"type": "webhook", "url": "https://runner.example.com/case", "timeout_s": 30.0}),
    (TracesRunner, {"type": "traces", "match": "contains", "lookback_days": 30}),
])
def test_schema_roundtrip(model, data):
    m = model.model_validate(data)
    assert model.model_validate_json(m.model_dump_json()) == m
    assert m.model_dump(mode="json") == data


def test_integration_created_out_roundtrip():
    base = {"id": "i", "org_id": "o", "kind": "webhook", "name": "H", "config": {}, "enabled": True,
            "last_sync_at": None, "last_status": "unknown", "error_count": 0, "last_error": "",
            "created_at": "2026-10-21T00:00:00"}
    m = IntegrationCreatedOut.model_validate({**base, "signing_secret": "ab" * 32})
    assert IntegrationCreatedOut.model_validate_json(m.model_dump_json()) == m
    assert "signing_secret" not in IntegrationOut.model_fields


@pytest.mark.parametrize("data", [{"email": "a@b.co", "role": "viewer", "status": "sent", "invite_id": None,
                                   "expires_at": None}])
def test_invite_out_rejects_unknown_status(data):
    with pytest.raises(ValidationError):
        InviteOut.model_validate(data)


def test_runner_defaults():
    assert runner_adapter.validate_python({"type": "webhook", "url": "http://r/x"}) == \
        WebhookRunner(type="webhook", url="http://r/x", timeout_s=10.0)
    assert runner_adapter.validate_python({"type": "traces"}) == \
        TracesRunner(type="traces", match="exact", lookback_days=7)


@pytest.mark.parametrize("data", [
    {"type": "llm"},
    {"url": "https://r/x"},
    {"type": "webhook"},
    {"type": "webhook", "url": "ftp://r/x"},
    {"type": "webhook", "url": "r/x"},
    {"type": "webhook", "url": "https://r/" + "x" * 2048},
    {"type": "webhook", "url": "https://r/x", "timeout_s": 0},
    {"type": "webhook", "url": "https://r/x", "timeout_s": 61},
    {"type": "traces", "match": "fuzzy"},
    {"type": "traces", "lookback_days": 0},
    {"type": "traces", "lookback_days": 91},
])
def test_runner_rejects(data):
    with pytest.raises(ValidationError):
        runner_adapter.validate_python(data)


def test_eval_set_schemas_carry_the_runner():
    assert EvalSetCreate(name="s").runner is None
    c = EvalSetCreate.model_validate({"name": "s", "runner": {"type": "traces"}})
    assert c.runner == TracesRunner(type="traces")
    assert "runner" not in EvalSetUpdate().model_fields_set
    assert EvalSetUpdate.model_validate({"runner": None}).model_fields_set == {"runner"}
    out = EvalSetOut.model_validate({"id": "s", "org_id": "o", "name": "n", "description": "", "evaluators": [],
                                     "schedule_cron": None, "runner": {"type": "webhook", "url": "https://r"},
                                     "created_at": datetime(2026, 10, 21)})
    assert EvalSetOut.model_validate_json(out.model_dump_json()) == out
    assert "runner" in EvalSetOut.model_json_schema()["required"]


# --- the eval runner is stored and returned (calling it is SG-P2's) ---

async def test_eval_set_runner_through_the_api(client, db, auth_headers):
    hook = {"type": "webhook", "url": "https://runner.example.com/case"}
    r = await client.post("/api/evaluations/sets", json={"name": "s", "runner": hook, "cases": [{"input": "q"}]},
                          headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["case_count"] == 1 and [c["input"] for c in r.json()["cases"]] == ["q"]
    sid = r.json()["id"]
    assert r.json()["runner"] == {**hook, "timeout_s": 10.0}
    assert (await client.get(f"/api/evaluations/sets/{sid}", headers=auth_headers)).json()["runner"]["type"] == \
        "webhook"
    stored = (await db.execute(sa.select(EvaluationSet.runner).where(EvaluationSet.id == sid))).scalar_one()
    assert stored == {**hook, "timeout_s": 10.0}

    r = await client.patch(f"/api/evaluations/sets/{sid}", json={"name": "renamed"}, headers=auth_headers)
    assert r.json()["runner"]["type"] == "webhook"  # absent: unchanged
    r = await client.patch(f"/api/evaluations/sets/{sid}", json={"runner": {"type": "traces", "match": "contains"}},
                           headers=auth_headers)
    assert r.json()["runner"] == {"type": "traces", "match": "contains", "lookback_days": 7}
    r = await client.patch(f"/api/evaluations/sets/{sid}", json={"runner": None}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["runner"] is None
    listed = (await client.get("/api/evaluations/sets", headers=auth_headers)).json()
    assert [s["runner"] for s in listed] == [None]


async def test_eval_set_without_runner_and_bad_runner(client, auth_headers):
    r = await client.post("/api/evaluations/sets", json={"name": "s"}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["runner"] is None
    r = await client.post("/api/evaluations/sets", json={"name": "s", "runner": {"type": "webhook", "url": "x"}},
                          headers=auth_headers)
    assert r.status_code == 422
    sid = (await client.get("/api/evaluations/sets", headers=auth_headers)).json()[0]["id"]
    r = await client.patch(f"/api/evaluations/sets/{sid}", json={"runner": {"type": "nope"}}, headers=auth_headers)
    assert r.status_code == 422


# --- storage ---

async def test_invite_and_oauth_state_storage(db, org, admin_user):
    org_id, user_id = org.id, admin_user.id
    exp = datetime.utcnow() + timedelta(hours=72)
    db.add(Invite(org_id=org.id, email="a@b.co", role="viewer", token_hash="h" * 64, invited_by=admin_user.id,
                  expires_at=exp))
    db.add(OAuthState(provider="slack", state_hash="s" * 64, org_id=org.id, user_id=admin_user.id,
                      expires_at=datetime.utcnow() + timedelta(minutes=10)))
    await db.commit()
    inv = (await db.execute(sa.select(Invite))).scalar_one()
    st = (await db.execute(sa.select(OAuthState))).scalar_one()
    assert inv.accepted_at is None and inv.created_at and inv.expires_at == exp
    assert st.used_at is None and st.created_at

    db.add(Invite(org_id=org_id, email="c@d.co", role="viewer", token_hash="h" * 64, expires_at=exp))
    with pytest.raises(sa.exc.IntegrityError):
        await db.commit()
    await db.rollback()
    db.add(OAuthState(provider="slack", state_hash="s" * 64, org_id=org_id, user_id=user_id, expires_at=exp))
    with pytest.raises(sa.exc.IntegrityError):
        await db.commit()


def test_storage_columns():
    for table, cols in {
        "invites": {"id", "org_id", "email", "role", "token_hash", "invited_by", "created_at", "expires_at",
                    "accepted_at"},
        "oauth_states": {"id", "provider", "state_hash", "org_id", "user_id", "created_at", "expires_at", "used_at"},
    }.items():
        t = Invite.metadata.tables[table]
        assert set(t.c.keys()) == cols
        assert {c.name for c in t.c if c.nullable} == ({"invited_by", "accepted_at"} if table == "invites"
                                                      else {"used_at"})
    assert Integration.__table__.c.signing_secret.nullable
    assert Integration.__table__.c.signing_secret.type.length == 64
    assert EvaluationSet.__table__.c.runner.nullable


# --- OpenAPI ---

def test_contract_routes_in_openapi():
    paths = app.openapi()["paths"]
    ref = lambda p, m: paths[p][m]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]  # noqa: E731
    assert ref("/api/auth/accept-invite", "post").endswith("/TokenPair")
    assert ref("/api/auth/password", "post").endswith("/TokenPair")
    assert ref("/api/integrations/{int_id}/signing-secret", "post").endswith("/SigningSecretOut")
    assert ref("/api/integrations", "post").endswith("/IntegrationCreatedOut")
    assert ref("/api/integrations/oauth/slack/start", "get").endswith("/SlackOAuthStartOut")
    assert ref("/api/integrations/oauth/slack/callback", "get").endswith("/SlackOAuthCallbackOut")
    assert "post" in paths["/api/integrations/webhooks/{kind}/{org_id}"]
    body = lambda p: paths[p]["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"]  # noqa: E731
    assert body("/api/auth/accept-invite").endswith("/AcceptInviteRequest")
    assert body("/api/auth/password").endswith("/PasswordChangeRequest")
    assert "security" not in paths["/api/auth/accept-invite"]["post"]
    assert paths["/api/auth/password"]["post"].get("security")
