"""SG-P1 platform security: invite tokens, webhook signatures, Slack OAuth state, outbound URLs, the live feed's auth,
startup secrets and refresh. Each item has the attack and the legitimate path."""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import re
import threading
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import sqlalchemy as sa
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app.routers.integrations as integrations_router
import app.routers.ws as ws_router
from app.config import Settings, settings
from app.main import app as fastapi_app, lifespan
from app.middleware.auth import decode_token, hash_token, make_access_token, make_refresh_token
from app.models import (
    AuditLog, Integration, Invite, OAuthState, Organization, OrgMembership, Session, User,
)
from app.services import auth_service, notification_service, outbound
from tests.conftest import _member

PASSWORD = "Secret1234!"


def _err(r):
    return r.json()["error"]["message"]


def _bearer(user, org, role):
    return {"Authorization": f"Bearer {make_access_token(user.id, org.id, role)}"}


async def _user(db, password=PASSWORD, email=None):
    u = User(email=email or f"u-{uuid.uuid4().hex[:8]}@example.com",
             password_hash=auth_service.hash_password(password), full_name="u")
    db.add(u)
    await db.commit()
    return u


async def _join(db, user, org, role):
    db.add(OrgMembership(org_id=org.id, user_id=user.id, role=role))
    await db.commit()


async def _org(db, name="Other"):
    o = Organization(name=name, slug=f"{name.lower()}-{uuid.uuid4().hex[:8]}")
    db.add(o)
    await db.commit()
    return o


async def _rows(db, model, *where):
    return (await db.execute(sa.select(model).where(*where))).scalars().all()


# --- 1. invites never set a known password ---

@pytest.fixture
def mail(monkeypatch):
    sent = []

    async def send(to, subject, body):
        sent.append((to, subject, body))
        return True
    monkeypatch.setattr(notification_service, "send_email", send)
    return sent


def _token(mail):
    return re.search(r"/accept-invite\?token=([\w-]+)", mail[-1][2])[1]


async def test_invite_of_a_new_email_stores_a_token_and_sends_it_only_by_email(client, db, org, admin_user,
                                                                                auth_headers, mail, monkeypatch):
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://app.example.com")
    r = await client.post("/api/auth/invite", json={"email": "new@example.com", "role": "viewer"},
                          headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "pending" and body["email"] == "new@example.com" and body["role"] == "viewer"
    (to, _, text), = mail
    assert to == "new@example.com" and "https://app.example.com/accept-invite?token=" in text
    token = _token(mail)
    assert token not in r.text and "password" not in r.text
    (inv,) = await _rows(db, Invite)
    assert inv.id == body["invite_id"] and inv.org_id == org.id and inv.invited_by == admin_user.id
    assert inv.token_hash == hashlib.sha256(token.encode()).hexdigest() and token not in inv.token_hash
    assert timedelta(hours=71, minutes=59) < inv.expires_at - datetime.utcnow() <= timedelta(hours=72)
    assert not await _rows(db, User, User.email == "new@example.com")  # no account with a known password
    r = await client.post("/api/auth/login", json={"email": "new@example.com", "password": "ChangeMe1234!"})
    assert r.status_code == 401


async def test_accept_invite_creates_the_account_and_signs_in_to_the_invite_org(client, db, org, auth_headers, mail):
    await client.post("/api/auth/invite", json={"email": "new@example.com", "role": "developer"},
                      headers=auth_headers)
    token = _token(mail)
    r = await client.post("/api/auth/accept-invite", json={"token": token, "password": PASSWORD})
    assert r.status_code == 200
    claims = decode_token(r.json()["access_token"])
    assert claims["org_id"] == org.id and claims["role"] == "developer"
    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"})
    assert me.json()["user"]["email"] == "new@example.com" and me.json()["role"] == "developer"
    (inv,) = await _rows(db, Invite)
    await db.refresh(inv)
    assert inv.accepted_at is not None
    r = await client.post("/api/auth/login", json={"email": "new@example.com", "password": PASSWORD})
    assert r.status_code == 200
    # single use
    r = await client.post("/api/auth/accept-invite", json={"token": token, "password": "Another1234!"})
    assert r.status_code == 400 and _err(r) == "invalid_invite"


async def test_accept_invite_refuses_unknown_expired_and_replaced_tokens(client, db, auth_headers, mail):
    async def invite():
        await client.post("/api/auth/invite", json={"email": "new@example.com", "role": "viewer"},
                          headers=auth_headers)
        return _token(mail)

    def accept(token):
        return client.post("/api/auth/accept-invite", json={"token": token, "password": PASSWORD})

    old = await invite()
    new = await invite()
    assert len(await _rows(db, Invite)) == 1  # replaced
    for token in (old, "x" * 43, hashlib.sha256(new.encode()).hexdigest()):
        r = await accept(token)
        assert r.status_code == 400 and _err(r) == "invalid_invite"
    await db.execute(sa.update(Invite).values(expires_at=datetime.utcnow() - timedelta(seconds=1)))
    await db.commit()
    r = await accept(new)
    assert r.status_code == 400 and _err(r) == "invalid_invite"
    assert not await _rows(db, User, User.email == "new@example.com")


async def test_accept_invite_after_the_email_registered_is_409(client, db, auth_headers, mail):
    await client.post("/api/auth/invite", json={"email": "new@example.com", "role": "viewer"},
                      headers=auth_headers)
    await _user(db, email="new@example.com")
    r = await client.post("/api/auth/accept-invite", json={"token": _token(mail), "password": PASSWORD})
    assert r.status_code == 409 and _err(r) == "email_exists"
    (inv,) = await _rows(db, Invite)
    assert inv.accepted_at is None


async def test_invite_of_an_existing_account_adds_it(client, db, org, auth_headers, mail):
    u = await _user(db)
    r = await client.post("/api/auth/invite", json={"email": u.email, "role": "viewer"}, headers=auth_headers)
    assert r.json() == {"email": u.email, "role": "viewer", "status": "added", "invite_id": None, "expires_at": None}
    r = await client.post("/api/auth/invite", json={"email": u.email, "role": "admin"}, headers=auth_headers)
    assert r.json()["status"] == "added"
    (m,) = await _rows(db, OrgMembership, OrgMembership.user_id == u.id)
    await db.refresh(m)
    assert (m.org_id, m.role) == (org.id, "admin")
    assert not await _rows(db, Invite) and not mail
    assert auth_service.verify_password(PASSWORD, (await db.get(User, u.id)).password_hash)


@pytest.mark.parametrize("role", ["owner", "superuser", ""])
async def test_invite_roles_are_limited(client, db, auth_headers, role, mail):
    r = await client.post("/api/auth/invite", json={"email": "new@example.com", "role": role}, headers=auth_headers)
    assert r.status_code == 422
    assert not await _rows(db, Invite) and not mail


async def test_an_admin_cannot_change_an_owners_role_by_invite(client, db, org, auth_headers, mail):
    owner = await _member(db, org, "owner")
    r = await client.post("/api/auth/invite", json={"email": owner.email, "role": "viewer"}, headers=auth_headers)
    assert r.status_code == 403 and _err(r) == "insufficient_role"
    (m,) = await _rows(db, OrgMembership, OrgMembership.user_id == owner.id)
    assert m.role == "owner"
    other = await _member(db, org, "owner")
    r = await client.post("/api/auth/invite", json={"email": owner.email, "role": "admin"},
                          headers=_bearer(other, org, "owner"))
    assert r.status_code == 200 and r.json()["status"] == "added"


async def test_invite_requires_an_admin(client, db, org, viewer_headers, mail):
    dev = await _member(db, org, "developer")
    for headers in (viewer_headers, _bearer(dev, org, "developer")):
        r = await client.post("/api/auth/invite", json={"email": "new@example.com", "role": "viewer"},
                              headers=headers)
        assert r.status_code == 403
    assert not await _rows(db, Invite) and not mail


async def test_password_change_checks_the_current_password_and_revokes_sessions(client, db, org):
    u = await _user(db)
    await _join(db, u, org, "developer")
    login = (await client.post("/api/auth/login", json={"email": u.email, "password": PASSWORD})).json()
    headers = {"Authorization": f"Bearer {login['access_token']}"}

    r = await client.post("/api/auth/password", json={"current_password": "wrong", "new_password": "New-pass1234"},
                          headers=headers)
    assert r.status_code == 400 and _err(r) == "wrong_password"
    assert (await client.post("/api/auth/login", json={"email": u.email, "password": PASSWORD})).status_code == 200

    r = await client.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": "New-pass1234"},
                          headers=headers)
    assert r.status_code == 200
    pair = r.json()
    assert decode_token(pair["access_token"])["org_id"] == org.id
    r = await client.post("/api/auth/refresh", json={"refresh_token": login["refresh_token"]})
    assert r.status_code == 401
    live = await _rows(db, Session, Session.user_id == u.id, Session.revoked_at.is_(None))
    assert [s.refresh_token_hash for s in live] == [hash_token(pair["refresh_token"])]
    assert (await client.post("/api/auth/refresh", json={"refresh_token": pair["refresh_token"]})).status_code == 200
    assert (await client.post("/api/auth/login", json={"email": u.email, "password": PASSWORD})).status_code == 401
    assert (await client.post("/api/auth/login", json={"email": u.email, "password": "New-pass1234"})).status_code == 200


# --- 2. the webhook receiver checks the signature ---

def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_signature_matches_the_contract_example():
    assert sign("a" * 64, b'{"event":"test"}') == \
        "sha256=668359ea785432c809ea605fdffed3351acabbb3b11c925f900366d28a1800dc"


@pytest.fixture
async def hook(client, auth_headers):
    r = await client.post("/api/integrations", json={"kind": "github", "name": "GH", "config": {}},
                          headers=auth_headers)
    return r.json()


def _post_hook(client, path, body: bytes, signature=None):
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["X-AgentOps-Signature"] = signature
    return client.post(path, content=body, headers=headers)


async def test_signed_webhook_is_accepted_and_audited(client, db, org, hook):
    assert re.fullmatch(r"[0-9a-f]{64}", hook["signing_secret"])
    body = b'{"event": "push", "n": 1}'
    r = await _post_hook(client, f"/api/integrations/webhooks/github/{org.id}", body, sign(hook["signing_secret"], body))
    assert r.status_code == 200 and r.json() == {"ok": True}
    (row,) = await _rows(db, AuditLog, AuditLog.action == "webhook_received")
    assert (row.org_id, row.resource_id, row.details) == (org.id, hook["id"], {"event": "push", "n": 1})


async def test_webhook_signature_example_secret(client, db, org):
    db.add(Integration(org_id=org.id, kind="webhook", name="W", config={}, signing_secret="a" * 64))
    await db.commit()
    r = await _post_hook(client, f"/api/integrations/webhooks/webhook/{org.id}", b'{"event":"test"}',
                         "sha256=668359ea785432c809ea605fdffed3351acabbb3b11c925f900366d28a1800dc")
    assert r.status_code == 200


async def test_webhook_refuses_every_bad_signature_alike(client, db, org, hook, auth_headers):
    secret, body = hook["signing_secret"], b'{"event": "push"}'
    other_org = await _org(db)
    foreign = Integration(org_id=other_org.id, kind="github", name="F", config={}, signing_secret="f" * 64)
    unsigned = Integration(org_id=org.id, kind="github", name="Old", config={})  # made before SG-P1: no secret
    db.add_all([foreign, unsigned])
    await db.commit()
    path = f"/api/integrations/webhooks/github/{org.id}"
    cases = [
        (path, body, None),
        (path, body, ""),
        (path, body, "sha256="),
        (path, body, sign(secret, body)[len("sha256="):]),  # no prefix
        (path, body, sign(secret, body).upper()),
        (path, body, sign(secret, b'{"event": "other"}')),  # signature of another body
        (path, body + b" ", sign(secret, body)),
        (path, body, sign("f" * 64, body)),  # another org's secret
        (path, body, sign("", body)),  # the integration without a secret
        (path, body, "sha256=é".encode()),
        (f"/api/integrations/webhooks/github/{uuid.uuid4()}", body, sign(secret, body)),  # unknown org
        (f"/api/integrations/webhooks/slack/{org.id}", body, sign(secret, body)),  # unknown kind
        (f"/api/integrations/webhooks/github/{other_org.id}", body, sign(secret, body)),  # right secret, other org
    ]
    for p, b, s in cases:
        r = await _post_hook(client, p, b, s)
        assert (r.status_code, _err(r)) == (401, "invalid_signature"), (p, b, s)
    await client.patch(f"/api/integrations/{hook['id']}", json={"enabled": False}, headers=auth_headers)
    r = await _post_hook(client, path, body, sign(secret, body))
    assert r.status_code == 401  # disabled
    assert not await _rows(db, AuditLog, AuditLog.action == "webhook_received")


@pytest.mark.parametrize("body", [b"[1, 2]", b'"x"', b"not json", b""])
async def test_signed_body_that_is_not_an_object_is_422(client, db, org, hook, body):
    r = await _post_hook(client, f"/api/integrations/webhooks/github/{org.id}", body, sign(hook["signing_secret"], body))
    assert r.status_code == 422
    assert not await _rows(db, AuditLog, AuditLog.action == "webhook_received")


async def test_webhook_matches_any_enabled_integration_of_the_kind(client, db, org, hook, auth_headers):
    second = (await client.post("/api/integrations", json={"kind": "github", "name": "GH2", "config": {}},
                                headers=auth_headers)).json()
    assert second["signing_secret"] != hook["signing_secret"]
    body = b"{}"
    r = await _post_hook(client, f"/api/integrations/webhooks/github/{org.id}", body, sign(second["signing_secret"], body))
    assert r.status_code == 200
    (row,) = await _rows(db, AuditLog, AuditLog.action == "webhook_received")
    assert row.resource_id == second["id"]


async def test_signing_secret_rotation(client, db, org, hook, auth_headers):
    path, body = f"/api/integrations/webhooks/github/{org.id}", b"{}"
    r = await client.post(f"/api/integrations/{hook['id']}/signing-secret", headers=auth_headers)
    assert r.status_code == 200
    new = r.json()["signing_secret"]
    assert re.fullmatch(r"[0-9a-f]{64}", new) and new != hook["signing_secret"]
    assert (await _post_hook(client, path, body, sign(hook["signing_secret"], body))).status_code == 401
    assert (await _post_hook(client, path, body, sign(new, body))).status_code == 200
    listed = (await client.get("/api/integrations", headers=auth_headers)).text
    assert new not in listed and hook["signing_secret"] not in listed


async def test_signing_secret_rotation_is_org_scoped(client, db, org, auth_headers):
    other = await _org(db)
    i = Integration(org_id=other.id, kind="github", name="F", config={}, signing_secret="f" * 64)
    db.add(i)
    await db.commit()
    r = await client.post(f"/api/integrations/{i.id}/signing-secret", headers=auth_headers)
    assert r.status_code == 404
    await db.refresh(i)
    assert i.signing_secret == "f" * 64


# --- 3. Slack OAuth state ---

SLACK_OK = {"ok": True, "team": {"name": "Acme"}, "incoming_webhook": {"url": "https://hooks.slack.com/services/x"}}


@pytest.fixture
def slack(monkeypatch):
    monkeypatch.setattr(settings, "SLACK_CLIENT_ID", "cid")
    monkeypatch.setattr(settings, "SLACK_CLIENT_SECRET", "csecret")
    codes = []

    async def exchange(code):
        codes.append(code)
        return SLACK_OK
    monkeypatch.setattr(integrations_router, "_slack_exchange", exchange)
    return codes


async def _start(client, headers):
    r = await client.get("/api/integrations/oauth/slack/start", headers=headers)
    assert r.status_code == 200
    return parse_qs(urlparse(r.json()["url"]).query)["state"][0]


def _callback(client, state, code="c1"):
    return client.get("/api/integrations/oauth/slack/callback", params={"code": code, "state": state})


async def test_slack_oauth_state_is_a_single_use_nonce_bound_to_the_admin(client, db, org, admin_user, auth_headers,
                                                                         slack):
    state = await _start(client, auth_headers)
    assert state != org.id and len(state) >= 40
    (row,) = await _rows(db, OAuthState)
    assert (row.provider, row.org_id, row.user_id) == ("slack", org.id, admin_user.id)
    assert row.state_hash == hash_token(state)
    assert timedelta(minutes=9) < row.expires_at - datetime.utcnow() <= timedelta(minutes=10)

    r = await _callback(client, state)
    assert r.status_code == 200 and r.json() == {"ok": True, "team": "Acme"}
    (i,) = await _rows(db, Integration)
    assert (i.org_id, i.kind, i.config["webhook_url"]) == (org.id, "slack", SLACK_OK["incoming_webhook"]["url"])
    assert i.signing_secret
    r = await _callback(client, state, "c2")
    assert r.status_code == 400 and _err(r) == "invalid_state"
    assert slack == ["c1"] and len(await _rows(db, Integration)) == 1


async def test_slack_callback_refuses_a_forged_state(client, db, org, auth_headers, slack):
    await _start(client, auth_headers)
    for state in (org.id, "x" * 43, hash_token("x")):  # the org id was the old state
        r = await _callback(client, state)
        assert r.status_code == 400 and _err(r) == "invalid_state"
    assert slack == [] and not await _rows(db, Integration)


async def test_slack_callback_refuses_an_expired_state(client, db, auth_headers, slack):
    state = await _start(client, auth_headers)
    await db.execute(sa.update(OAuthState).values(expires_at=datetime.utcnow() - timedelta(seconds=1)))
    await db.commit()
    r = await _callback(client, state)
    assert r.status_code == 400 and _err(r) == "invalid_state" and slack == []


@pytest.mark.parametrize("change", ["demoted", "removed", "deactivated"])
async def test_slack_callback_refuses_a_starter_who_is_no_longer_admin(client, db, org, admin_user, auth_headers,
                                                                      slack, change):
    state = await _start(client, auth_headers)
    if change == "demoted":
        await db.execute(sa.update(OrgMembership).values(role="viewer"))
    elif change == "removed":
        await db.execute(sa.delete(OrgMembership))
    else:
        await db.execute(sa.update(User).values(is_active=False))
    await db.commit()
    r = await _callback(client, state)
    assert r.status_code == 400 and _err(r) == "invalid_state"
    assert slack == [] and not await _rows(db, Integration)


async def test_slack_state_from_another_org_creates_in_the_starters_org(client, db, org, auth_headers, slack):
    other = await _org(db)
    other_admin = await _member(db, other, "owner")
    state = await _start(client, _bearer(other_admin, other, "owner"))
    assert (await _callback(client, state)).status_code == 200
    (i,) = await _rows(db, Integration)
    assert i.org_id == other.id


async def test_slack_start_cleans_up_expired_states(client, db, org, admin_user, auth_headers, slack, viewer_headers):
    past = datetime.utcnow() - timedelta(minutes=1)
    db.add(OAuthState(provider="slack", state_hash="e" * 64, org_id=org.id, user_id=admin_user.id, expires_at=past))
    await db.commit()
    state = await _start(client, auth_headers)
    assert [r.state_hash for r in await _rows(db, OAuthState)] == [hash_token(state)]
    r = await client.get("/api/integrations/oauth/slack/start", headers=viewer_headers)
    assert r.status_code == 403


# --- 4. outbound URLs refuse private addresses after resolution ---

@pytest.mark.parametrize("ip", [
    "127.0.0.1", "127.8.9.10", "10.0.0.5", "172.16.3.4", "192.168.1.1", "169.254.169.254", "100.64.0.1",
    "0.0.0.0", "224.0.0.1", "255.255.255.255", "::1", "::", "fe80::1", "fc00::1", "fd12::1", "::ffff:127.0.0.1",
    "::ffff:169.254.169.254", "ff02::1",
])
def test_private_addresses_are_not_public(ip):
    assert not outbound.is_public(ipaddress.ip_address(ip))


@pytest.mark.parametrize("ip", ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111", "::ffff:8.8.8.8"])
def test_public_addresses_are_public(ip):
    assert outbound.is_public(ipaddress.ip_address(ip))


@pytest.fixture
def net(monkeypatch):
    """A fake resolver (`answers` per host, in order) and a transport recording every request."""
    state = {"answers": {}, "lookups": [], "requests": []}
    real = outbound._resolve

    async def resolve(host, port):
        state["lookups"].append(host)
        if host not in state["answers"]:
            return await real(host, port)
        answers = state["answers"][host]
        return answers.pop(0) if len(answers) > 1 else answers[0]

    def handler(request):
        state["requests"].append(request)
        return httpx.Response(200, json={"ok": True})
    monkeypatch.setattr(outbound, "_resolve", resolve)
    monkeypatch.setattr(outbound, "_transport", httpx.MockTransport(handler))
    return state


@pytest.mark.parametrize("answer", [["169.254.169.254"], ["10.1.2.3"], ["93.184.216.34", "127.0.0.1"], ["::1"], []])
async def test_outbound_refuses_a_host_resolving_to_a_private_address(net, answer):
    net["answers"]["hooks.example.com"] = [answer]
    with pytest.raises(outbound.BlockedURL):
        await outbound.post("https://hooks.example.com/x", json={})
    assert net["requests"] == []


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:6379/", "http://localhost/", "http://[::1]/", "http://2130706433/", "http://0x7f.1/",
    "http://169.254.169.254/latest/meta-data/", "http://[::ffff:127.0.0.1]/", "file:///etc/passwd",
    "gopher://example.com/", "ftp://example.com/", "http:///x", "not a url",
])
async def test_outbound_refuses_private_literals_and_other_schemes(url, monkeypatch):
    sent = []
    monkeypatch.setattr(outbound, "_transport", httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200)))
    with pytest.raises(outbound.BlockedURL):
        await outbound.post(url, json={})
    assert sent == []


async def test_outbound_connects_to_the_address_it_checked(net):
    # DNS rebinding: the second answer would be the metadata service; the request goes to the first, checked one
    net["answers"]["hooks.example.com"] = [["93.184.216.34"], ["169.254.169.254"]]
    r = await outbound.post("https://hooks.example.com:8443/in?x=1", json={"a": 1})
    assert r.status_code == 200
    (req,) = net["requests"]
    assert net["lookups"] == ["hooks.example.com"]
    assert str(req.url) == "https://93.184.216.34:8443/in?x=1"
    assert req.headers["host"] == "hooks.example.com:8443"
    assert req.extensions["sni_hostname"] == "hooks.example.com"
    assert json.loads(req.content) == {"a": 1}


async def test_outbound_pins_ipv6_and_plain_http(net):
    net["answers"]["v6.example.com"] = [["2606:4700:4700::1111"]]
    await outbound.post("http://v6.example.com/x", json={})
    (req,) = net["requests"]
    assert str(req.url) == "http://[2606:4700:4700::1111]/x" and req.headers["host"] == "v6.example.com"


async def test_outbound_does_not_follow_redirects(monkeypatch, net):
    net["answers"]["hooks.example.com"] = [["93.184.216.34"]]
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})
    monkeypatch.setattr(outbound, "_transport", httpx.MockTransport(handler))
    r = await outbound.post("https://hooks.example.com/x", json={})
    assert r.status_code == 302 and len(sent) == 1


async def test_outbound_reaches_a_real_server_at_the_pinned_address(monkeypatch):
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["host"], seen["path"] = self.headers["Host"], self.path
            seen["body"] = self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    port = server.server_address[1]

    async def resolve(host, p):
        assert (host, p) == ("hooks.internal.test", port)
        return ["127.0.0.1"]
    monkeypatch.setattr(outbound, "_resolve", resolve)
    monkeypatch.setattr(outbound, "is_public", lambda ip: True)  # loopback stands in for a public server
    r = await outbound.post(f"http://hooks.internal.test:{port}/in", json={"a": 1})
    server.server_close()
    assert r.status_code == 204
    assert seen == {"host": f"hooks.internal.test:{port}", "path": "/in", "body": b'{"a": 1}'}


async def test_alert_channels_refuse_private_targets(net):
    net["answers"]["meta.example.com"] = [["169.254.169.254"]]
    net["answers"]["hooks.example.com"] = [["93.184.216.34"]]
    channels = [{"type": "webhook", "target": "http://meta.example.com/x"},
                {"type": "slack", "target": "http://127.0.0.1:8080/"},
                {"type": "webhook", "url": "https://hooks.example.com/ok"},
                {"type": "slack", "target": "https://hooks.example.com/slack"}]
    assert await notification_service.dispatch_alert(channels, "m", "high") == [False, False, True, True]
    assert [str(r.url) for r in net["requests"]] == ["https://93.184.216.34/ok", "https://93.184.216.34/slack"]
    assert json.loads(net["requests"][0].content) == {"message": "m", "severity": "high"}
    assert json.loads(net["requests"][1].content) == {"text": "m"}


@pytest.mark.parametrize("kind,config", [
    ("webhook", {"url": "http://meta.example.com/latest"}),
    ("webhook", {"url": "http://127.0.0.1:6379/"}),
    ("slack", {"webhook_url": "http://meta.example.com/x"}),
])
async def test_integration_test_connection_refuses_private_targets(client, db, org, auth_headers, net, kind, config):
    net["answers"]["meta.example.com"] = [["169.254.169.254"]]
    i = Integration(org_id=org.id, kind=kind, name="I", config=config)
    db.add(i)
    await db.commit()
    r = await client.post(f"/api/integrations/{i.id}/test", headers=auth_headers)
    assert r.json()["ok"] is False and r.json()["message"] == "blocked_address"
    assert net["requests"] == []


@pytest.mark.parametrize("kind,config", [("webhook", {"url": "https://hooks.example.com/x"}),
                                         ("slack", {"webhook_url": "https://hooks.example.com/x"})])
async def test_integration_test_connection_reaches_public_targets(client, db, org, auth_headers, net, kind, config):
    net["answers"]["hooks.example.com"] = [["93.184.216.34"]]
    i = Integration(org_id=org.id, kind=kind, name="I", config=config)
    db.add(i)
    await db.commit()
    r = await client.post(f"/api/integrations/{i.id}/test", headers=auth_headers)
    assert r.json()["ok"] is True
    (req,) = net["requests"]
    assert req.url.host == "93.184.216.34" and req.headers["host"] == "hooks.example.com"


# --- 5. /ws/traces requires an access token and a current membership ---

def _refused(app_, token):
    with TestClient(app_) as tc:
        with pytest.raises(WebSocketDisconnect) as e, tc.websocket_connect(f"/ws/traces?token={token}"):
            pass
    return e.value.code


def test_ws_feed_refuses_tokens_that_are_not_a_members_access_token(app_, org, admin_user, session_maker):
    refresh, _ = make_refresh_token(admin_user.id, org.id)
    assert _refused(app_, refresh) == 4401
    assert _refused(app_, "garbage") == 4401
    with TestClient(app_) as tc:
        with pytest.raises(WebSocketDisconnect), tc.websocket_connect("/ws/traces"):
            pass

    async def setup():
        async with session_maker() as s:
            other = Organization(name="O", slug=f"o-{uuid.uuid4().hex[:8]}")
            s.add(other)
            await s.commit()
            return other
    other = asyncio.run(setup())
    assert _refused(app_, make_access_token(admin_user.id, other.id, "admin")) == 4401  # not a member there

    async def deactivate():
        async with session_maker() as s:
            await s.execute(sa.update(User).values(is_active=False))
            await s.commit()
    asyncio.run(deactivate())
    assert _refused(app_, make_access_token(admin_user.id, org.id, "admin")) == 4401


def test_ws_feed_closes_when_the_membership_ends(app_, org, viewer_user, session_maker, monkeypatch, redis):
    monkeypatch.setattr(ws_router, "FEED_RECHECK_S", 0.05)
    token = make_access_token(viewer_user.id, org.id, "viewer")
    with TestClient(app_) as tc, tc.websocket_connect(f"/ws/traces?token={token}") as sock:
        assert sock.receive_json() == {"type": "ping"}  # a member: still connected

        async def leave():
            async with session_maker() as s:
                await s.execute(sa.delete(OrgMembership))
                await s.commit()
        asyncio.run(leave())
        with pytest.raises(WebSocketDisconnect) as e:
            for _ in range(200):
                sock.receive_json()
        assert e.value.code == 4401


def test_ws_feed_serves_a_viewer(app_, org, viewer_user, redis):
    token = make_access_token(viewer_user.id, org.id, "viewer")
    with TestClient(app_) as tc, tc.websocket_connect(f"/ws/traces?token={token}") as sock:
        tc.portal.call(redis.publish, f"traces:{org.id}", json.dumps({"id": "t1"}))
        assert sock.receive_json() == {"type": "trace", "data": {"id": "t1"}}


# --- 6. no default secrets outside development ---

DEFAULTS = {k: Settings.model_fields[k].default for k in ("APP_SECRET", "JWT_SECRET")}
STRONG = {"APP_SECRET": "s" * 48, "JWT_SECRET": "j" * 48}


@pytest.mark.parametrize("env,secrets", [
    ("production", DEFAULTS),
    ("production", {**STRONG, "JWT_SECRET": DEFAULTS["JWT_SECRET"]}),
    ("production", {**STRONG, "APP_SECRET": DEFAULTS["APP_SECRET"]}),
    ("staging", DEFAULTS),
    ("test", DEFAULTS),
    ("production", {"APP_SECRET": "change-me-please-32-bytes-minimum-secret-key",
                    "JWT_SECRET": "change-me-too-please-32-bytes-minimum-jwt-key"}),  # .env.example
    ("production", {**STRONG, "JWT_SECRET": ""}),
])
async def test_app_refuses_to_start_with_default_secrets(monkeypatch, redis, env, secrets):
    monkeypatch.setattr(settings, "APP_ENV", env)
    for k, v in secrets.items():
        monkeypatch.setattr(settings, k, v)
    with pytest.raises(RuntimeError, match="SECRET"):
        async with lifespan(fastapi_app):
            pass


@pytest.mark.parametrize("env,secrets", [("development", DEFAULTS), ("production", STRONG)])
async def test_app_starts_with_its_own_secrets_or_in_development(monkeypatch, redis, env, secrets):
    monkeypatch.setattr(settings, "APP_ENV", env)
    for k, v in secrets.items():
        monkeypatch.setattr(settings, k, v)
    async with lifespan(fastapi_app):
        pass


# --- 7. refresh keeps the session's org ---

async def _session(db, user, org):
    refresh, exp = make_refresh_token(user.id, org.id)
    db.add(Session(user_id=user.id, refresh_token_hash=hash_token(refresh), expires_at=exp))
    await db.commit()
    return refresh


async def test_refresh_keeps_the_org_the_session_was_issued_for(client, db, org):
    u = await _user(db)
    await _join(db, u, org, "admin")  # first membership: the one login and the old refresh picked
    second = await _org(db, "Second")
    await _join(db, u, second, "viewer")
    refresh = await _session(db, u, second)
    for _ in range(2):  # and the rotated token keeps it too
        r = await client.post("/api/auth/refresh", json={"refresh_token": refresh})
        assert r.status_code == 200
        claims = decode_token(r.json()["access_token"])
        assert (claims["org_id"], claims["role"]) == (second.id, "viewer")
        refresh = r.json()["refresh_token"]
        assert decode_token(refresh)["org_id"] == second.id


async def test_refresh_after_leaving_the_sessions_org_is_refused(client, db, org):
    u = await _user(db)
    await _join(db, u, org, "admin")
    second = await _org(db, "Second")
    await _join(db, u, second, "viewer")
    refresh = await _session(db, u, second)
    await db.execute(sa.delete(OrgMembership).where(OrgMembership.org_id == second.id))
    await db.commit()
    r = await client.post("/api/auth/refresh", json={"refresh_token": refresh})
    assert r.status_code == 401 and _err(r) == "no_org"


async def test_refresh_uses_the_current_role(client, db, org):
    u = await _user(db)
    await _join(db, u, org, "admin")
    refresh = await _session(db, u, org)
    await db.execute(sa.update(OrgMembership).values(role="viewer"))
    await db.commit()
    r = await client.post("/api/auth/refresh", json={"refresh_token": refresh})
    assert decode_token(r.json()["access_token"])["role"] == "viewer"


async def test_refresh_token_without_an_org_is_refused(client, db, org):
    from jose import jwt
    u = await _user(db)
    await _join(db, u, org, "admin")
    legacy = jwt.encode({"sub": u.id, "exp": datetime.utcnow() + timedelta(days=1), "type": "refresh"},
                        settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    db.add(Session(user_id=u.id, refresh_token_hash=hash_token(legacy), expires_at=datetime.utcnow() + timedelta(days=1)))
    await db.commit()
    r = await client.post("/api/auth/refresh", json={"refresh_token": legacy})
    assert r.status_code == 401 and _err(r) == "invalid_session"


async def test_login_issues_a_refresh_token_for_its_org(client, db, org):
    u = await _user(db)
    await _join(db, u, org, "developer")
    r = await client.post("/api/auth/login", json={"email": u.email, "password": PASSWORD})
    assert decode_token(r.json()["refresh_token"])["org_id"] == org.id
    r = await client.post("/api/auth/refresh", json={"refresh_token": r.json()["refresh_token"]})
    assert decode_token(r.json()["access_token"])["org_id"] == org.id
