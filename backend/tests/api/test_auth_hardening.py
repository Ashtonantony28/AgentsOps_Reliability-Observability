"""SG-AP1.2: password hashing on its own bounded executor, login limited per address and per email, unknown emails
verified against a dummy hash, refresh tokens single use under concurrency."""
import asyncio
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.middleware.auth import hash_token, make_access_token, make_refresh_token
from app.models import Invite, OrgMembership, Session, User
from app.services import auth_service

PASSWORD = "Secret1234!"
HASHED = auth_service.hash_password(PASSWORD)


def _err(r):
    return r.json()["error"]["message"]


async def _user(db, org=None):
    u = User(email=f"u-{uuid.uuid4().hex[:8]}@example.com", password_hash=HASHED, full_name="u")
    db.add(u)
    await db.commit()
    if org:
        db.add(OrgMembership(org_id=org.id, user_id=u.id, role="admin"))
        await db.commit()
    return u


def _login(client, email, password=PASSWORD):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def _from(app_, host):
    return AsyncClient(transport=ASGITransport(app=app_, client=(host, 1234)), base_url="http://test")


@pytest.fixture
def clock(monkeypatch):
    now = [7_200_000.0]  # the start of a minute (and an hour) window
    monkeypatch.setattr(time, "time", lambda: now[0])
    return now


@pytest.fixture
def hashed_in(monkeypatch):
    """The threads each password hash and verify ran in."""
    threads = []
    for name in ("hash_password", "verify_password"):
        real = getattr(auth_service, name)
        monkeypatch.setattr(auth_service, name,
                            lambda *a, real=real: threads.append(threading.current_thread().name) or real(*a))
    return threads


# --- bcrypt on its own executor ---

async def test_every_password_hash_runs_on_the_password_executor(client, db, org, hashed_in):
    creds = {"email": "own-pool@example.com", "password": PASSWORD}
    assert (await client.post("/api/auth/register", json=creds)).status_code == 200  # hash, then login's verify
    r = await client.post("/api/auth/login", json=creds)
    assert r.status_code == 200
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.post("/api/auth/password", headers=headers,
                          json={"current_password": PASSWORD, "new_password": "New-pass1234"})
    assert r.status_code == 200  # verify, then hash
    token = secrets.token_urlsafe(32)
    db.add(Invite(org_id=org.id, email="invited@example.com", role="viewer", token_hash=hash_token(token),
                  expires_at=datetime.utcnow() + timedelta(hours=1)))
    await db.commit()
    r = await client.post("/api/auth/accept-invite", json={"token": token, "password": PASSWORD})
    assert r.status_code == 200
    assert (await _login(client, "nobody@example.com")).status_code == 401  # the dummy verify
    assert len(hashed_in) == 7
    assert all(n.startswith("password-hash") for n in hashed_in), hashed_in


def test_password_executor_is_bounded():
    pool = auth_service._hash_pool
    assert isinstance(pool, ThreadPoolExecutor) and pool._max_workers == settings.PASSWORD_HASH_THREADS


async def test_hashes_run_while_the_default_executor_is_full():
    # the hash alone: Postgres connects resolve their host on the default executor too
    release = threading.Event()
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(1))
    held = asyncio.ensure_future(asyncio.to_thread(release.wait, 10))
    try:
        assert await asyncio.wait_for(auth_service._hashed(auth_service.verify_password, PASSWORD, HASHED), 5)
    finally:
        release.set()
        await held


async def test_full_password_queue_is_refused_not_queued(client, db, org, monkeypatch):
    monkeypatch.setattr(settings, "PASSWORD_HASH_QUEUE", 3)
    monkeypatch.setattr(settings, "LOGIN_LIMIT_PER_MIN", 0)
    release, started = threading.Event(), []

    def blocked_verify(pw, hashed):
        started.append(1)
        release.wait(10)
        return False
    monkeypatch.setattr(auth_service, "verify_password", blocked_verify)
    u = await _user(db, org)
    tasks = [asyncio.create_task(_login(client, u.email, "wrong")) for _ in range(3)]
    try:
        for _ in range(100):
            if len(started) == settings.PASSWORD_HASH_THREADS:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        r = await _login(client, u.email, "wrong")
        assert r.status_code == 429 and _err(r) == "rate_limited"
        assert len(started) == settings.PASSWORD_HASH_THREADS  # the refused one never reached the pool
    finally:
        release.set()
    assert [r.status_code for r in await asyncio.gather(*tasks)] == [401] * 3  # queued ones still answered
    monkeypatch.setattr(auth_service, "verify_password", lambda pw, h: True)
    assert (await _login(client, u.email)).status_code == 200  # the queue drained


async def test_ingest_parsing_runs_while_the_password_executor_is_full(client, db, org, agent, api_key_headers,
                                                                        monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(auth_service, "verify_password", lambda pw, h: release.wait(10) and False)
    monkeypatch.setattr(settings, "LOGIN_LIMIT_PER_MIN", 0)
    u = await _user(db, org)
    logins = [asyncio.create_task(_login(client, u.email, "wrong")) for _ in range(settings.PASSWORD_HASH_THREADS)]
    try:
        await asyncio.sleep(0.05)
        r = await asyncio.wait_for(client.post("/api/traces/ingest", headers=api_key_headers,
                                               json={"agent_id": agent.id, "input": "q", "output": "a"}), 5)
        assert r.status_code == 202
    finally:
        release.set()
    assert {r.status_code for r in await asyncio.gather(*logins)} == {401}


# --- login limits ---

async def test_logins_are_limited_per_client_address(client, app_, db, org, clock, hashed_in):
    n = settings.LOGIN_LIMIT_PER_MIN
    codes = [(await _login(client, f"nobody-{i}@example.com")).status_code for i in range(n + 1)]
    assert codes == [401] * n + [429]
    assert len(hashed_in) == n  # counted before the verify
    u = await _user(db, org)
    r = await _login(client, u.email)
    assert r.status_code == 429 and _err(r) == "rate_limited"  # the right password too
    async with _from(app_, "10.0.0.2") as other:
        assert (await _login(other, u.email)).status_code == 200
    clock[0] += 59
    assert (await _login(client, u.email)).status_code == 429
    clock[0] += 1
    assert (await _login(client, u.email)).status_code == 200


async def test_logins_are_limited_per_email_and_address(app_, db, org, clock, hashed_in):
    u, other = await _user(db, org), await _user(db, org)
    n = settings.LOGIN_LIMIT_PER_MIN
    async with _from(app_, "10.1.0.1") as c:
        codes = [(await _login(c, u.email.upper() if i % 2 else u.email, "wrong")).status_code for i in range(n)]
        assert codes == [401] * n
        r = await _login(c, u.email)
        assert r.status_code == 429 and _err(r) == "rate_limited"
    assert len(hashed_in) == n
    async with _from(app_, "10.2.0.1") as c:
        assert (await _login(c, u.email)).status_code == 200  # the same email from another address
        assert (await _login(c, other.email)).status_code == 200
    clock[0] += 60
    async with _from(app_, "10.1.0.1") as c:
        assert (await _login(c, u.email)).status_code == 200


async def test_wrong_logins_from_many_addresses_do_not_lock_the_email(app_, db, org, clock):
    u = await _user(db, org)
    for i in range(3):
        async with _from(app_, f"10.3.0.{i}") as c:
            for _ in range(settings.LOGIN_LIMIT_PER_MIN):
                assert (await _login(c, u.email, "wrong")).status_code == 401
    async with _from(app_, "10.4.0.1") as c:
        assert (await _login(c, u.email)).status_code == 200


async def test_logins_under_the_limit_succeed(client, db, org, clock):
    u = await _user(db, org)
    for _ in range(settings.LOGIN_LIMIT_PER_MIN - 1):
        assert (await _login(client, u.email, "wrong")).status_code == 401
    r = await _login(client, u.email)
    assert r.status_code == 200 and r.json()["access_token"]


async def test_login_limit_is_configurable(client, db, org, clock, monkeypatch):
    u = await _user(db, org)
    monkeypatch.setattr(settings, "LOGIN_LIMIT_PER_MIN", 1)
    assert [(await _login(client, u.email)).status_code for _ in range(2)] == [200, 429]
    monkeypatch.setattr(settings, "LOGIN_LIMIT_PER_MIN", 0)  # 0 turns it off
    assert (await _login(client, u.email)).status_code == 200


async def test_registration_does_not_spend_the_login_limit(client, clock):
    creds = {"email": "reg@example.com", "password": PASSWORD}
    assert (await client.post("/api/auth/register", json=creds)).status_code == 200
    for _ in range(settings.LOGIN_LIMIT_PER_MIN):
        assert (await client.post("/api/auth/login", json=creds)).status_code == 200


# --- password changes ---

def _change(client, headers, current="wrong"):
    return client.post("/api/auth/password", headers=headers,
                       json={"current_password": current, "new_password": "New-pass1234"})


def _auth(u, org):
    return {"Authorization": f"Bearer {make_access_token(u.id, org.id, 'admin')}"}


async def test_password_changes_are_limited_per_user(client, db, org, clock, hashed_in):
    u, other = await _user(db, org), await _user(db, org)
    n = settings.PASSWORD_CHANGE_LIMIT_PER_MIN
    codes = [(await _change(client, _auth(u, org))).status_code for _ in range(n + 1)]
    assert codes == [400] * n + [429]
    r = await _change(client, _auth(u, org), PASSWORD)
    assert r.status_code == 429 and _err(r) == "rate_limited"  # the right password too
    assert len(hashed_in) == n  # counted before the verify
    assert (await _change(client, _auth(other, org), PASSWORD)).status_code == 200  # another user, same address
    assert (await _login(client, u.email)).status_code == 200  # the user's logins are not spent
    clock[0] += 59
    assert (await _change(client, _auth(u, org), PASSWORD)).status_code == 429
    clock[0] += 1
    assert (await _change(client, _auth(u, org), PASSWORD)).status_code == 200


async def test_password_change_limit_is_configurable(client, db, org, clock, monkeypatch):
    u = await _user(db, org)
    monkeypatch.setattr(settings, "PASSWORD_CHANGE_LIMIT_PER_MIN", 1)
    assert [(await _change(client, _auth(u, org))).status_code for _ in range(2)] == [400, 429]
    monkeypatch.setattr(settings, "PASSWORD_CHANGE_LIMIT_PER_MIN", 0)  # 0 turns it off
    assert (await _change(client, _auth(u, org))).status_code == 400


# --- each caller's share of the hash queue ---

@pytest.fixture
def blocked(monkeypatch):
    """Verifies wait for the event it yields."""
    release = threading.Event()

    def verify(pw, hashed):
        release.wait(10)
        return pw == PASSWORD
    monkeypatch.setattr(auth_service, "verify_password", verify)
    monkeypatch.setattr(settings, "LOGIN_LIMIT_PER_MIN", 0)
    monkeypatch.setattr(settings, "PASSWORD_CHANGE_LIMIT_PER_MIN", 0)
    yield release
    release.set()


async def _held(n):
    for _ in range(500):
        if auth_service._hashes_in_flight == n:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{auth_service._hashes_in_flight} hashes in flight, waiting for {n}")


async def test_one_users_password_changes_hold_a_bounded_share_of_the_queue(client, db, org, blocked):
    u, other = await _user(db, org), await _user(db, org)
    n = settings.PASSWORD_HASH_PER_CALLER
    held = [asyncio.create_task(_change(client, _auth(u, org))) for _ in range(n)]
    await _held(n)
    r = await _change(client, _auth(u, org))
    assert r.status_code == 429 and _err(r) == "rate_limited"
    assert auth_service._hashes_in_flight == n  # the refused one took no place
    others = [asyncio.create_task(_change(client, _auth(other, org))),  # another user, the same address
              asyncio.create_task(_login(client, other.email))]
    await _held(n + 2)
    blocked.set()
    assert [r.status_code for r in await asyncio.gather(*held)] == [400] * n
    assert [r.status_code for r in await asyncio.gather(*others)] == [400, 200]
    assert not auth_service._caller_hashes  # every place given back
    assert (await _change(client, _auth(u, org), PASSWORD)).status_code == 200


async def test_one_addresss_logins_hold_a_bounded_share_of_the_queue(app_, db, org, blocked):
    u = await _user(db, org)
    n = settings.PASSWORD_HASH_PER_CALLER
    async with _from(app_, "10.5.0.1") as c, _from(app_, "10.5.0.2") as owner:
        held = [asyncio.create_task(_login(c, f"nobody-{i}@example.com")) for i in range(n)]
        await _held(n)
        r = await _login(c, u.email)
        assert r.status_code == 429 and _err(r) == "rate_limited"
        mine = asyncio.create_task(_login(owner, u.email))
        await _held(n + 1)
        blocked.set()
        assert [r.status_code for r in await asyncio.gather(*held)] == [401] * n
        assert (await mine).status_code == 200
        assert (await _login(c, u.email)).status_code == 200  # places given back


async def test_per_caller_share_is_configurable(client, db, org, blocked, monkeypatch):
    monkeypatch.setattr(settings, "PASSWORD_HASH_PER_CALLER", 1)
    u = await _user(db, org)
    held = asyncio.create_task(_login(client, u.email))
    await _held(1)
    assert (await _login(client, u.email)).status_code == 429
    blocked.set()
    assert (await held).status_code == 200


async def test_every_hash_is_charged_to_its_caller(app_, db, org, monkeypatch):
    callers, real = [], auth_service._hashed

    async def hashed(fn, *args, caller=None):
        callers.append(caller)
        return await real(fn, *args, caller=caller)
    monkeypatch.setattr(auth_service, "_hashed", hashed)
    async with _from(app_, "10.6.0.1") as c:
        creds = {"email": "charged@example.com", "password": PASSWORD}
        r = await c.post("/api/auth/register", json=creds)
        assert r.status_code == 200
        uid = (await c.get("/api/auth/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"})
               ).json()["user"]["id"]
        assert (await c.post("/api/auth/password", headers={"Authorization": f"Bearer {r.json()['access_token']}"},
                             json={"current_password": PASSWORD, "new_password": "New-pass1234"})).status_code == 200
        token = secrets.token_urlsafe(32)
        db.add(Invite(org_id=org.id, email="charged-invite@example.com", role="viewer",
                      token_hash=hash_token(token), expires_at=datetime.utcnow() + timedelta(hours=1)))
        await db.commit()
        assert (await c.post("/api/auth/accept-invite", json={"token": token, "password": PASSWORD})).status_code == 200
    ip, user = "ip:10.6.0.1", f"user:{uid}"
    assert callers == [ip, ip, user, user, ip]  # register, its login, the change's verify and hash, accept-invite


# --- unknown emails ---

async def test_unknown_email_is_verified_against_a_dummy_hash(client, monkeypatch):
    seen = []
    real = auth_service.verify_password
    monkeypatch.setattr(auth_service, "verify_password", lambda pw, h: seen.append((pw, h)) or real(pw, h))
    r = await _login(client, "nobody@example.com", PASSWORD)
    assert r.status_code == 401 and _err(r) == "invalid_credentials"
    assert seen == [(PASSWORD, auth_service.DUMMY_HASH)]
    assert not real(PASSWORD, auth_service.DUMMY_HASH)


def test_dummy_hash_costs_what_a_real_hash_costs():
    ctx = auth_service.pwd_ctx
    assert ctx.identify(auth_service.DUMMY_HASH) == "bcrypt" and not ctx.needs_update(auth_service.DUMMY_HASH)
    real = auth_service.hash_password(PASSWORD)
    assert auth_service.DUMMY_HASH.split("$")[2] == real.split("$")[2]  # same cost factor


async def test_unknown_and_registered_emails_take_the_same_time(client, db, org):
    u = await _user(db, org)

    async def took(email):
        t = time.perf_counter()
        assert (await _login(client, email, "wrong-password")).status_code == 401
        return time.perf_counter() - t
    known, unknown = await took(u.email), await took("nobody@example.com")
    assert unknown > 0.5 * known, (known, unknown)


async def test_inactive_user_is_refused_after_the_verify(client, db, org, hashed_in):
    u = await _user(db, org)
    u.is_active = False
    await db.commit()
    r = await _login(client, u.email)
    assert r.status_code == 401 and _err(r) == "invalid_credentials"
    assert len(hashed_in) == 1


# --- refresh single use ---

async def _refresh_token(db, org):
    u = await _user(db, org)
    token, exp = make_refresh_token(u.id, org.id)
    db.add(Session(user_id=u.id, refresh_token_hash=hash_token(token), expires_at=exp))
    await db.commit()
    return u, token


async def test_concurrent_refreshes_of_one_token_yield_one_session(client, db, org):
    u, token = await _refresh_token(db, org)
    uid = u.id
    rs = await asyncio.gather(*[client.post("/api/auth/refresh", json={"refresh_token": token}) for _ in range(5)])
    assert sorted(r.status_code for r in rs) == [200] + [401] * 4
    assert {_err(r) for r in rs if r.status_code == 401} == {"invalid_session"}
    db.expire_all()
    live = (await db.execute(select(Session).where(Session.user_id == uid, Session.revoked_at.is_(None)))).scalars()
    assert [s.refresh_token_hash for s in live] == [hash_token(next(r for r in rs if r.status_code == 200)
                                                               .json()["refresh_token"])]


async def test_refresh_is_single_use_when_reads_interleave(client, db, org, monkeypatch):
    """Both refreshes have read the live session and its membership before either revokes it."""
    u, token = await _refresh_token(db, org)
    real, read, both = AsyncSession.execute, [], asyncio.Event()

    async def execute(self, stmt, *a, **kw):
        out = await real(self, stmt, *a, **kw)
        if getattr(stmt, "is_select", False) and "org_memberships" in str(stmt):
            read.append(1)
            if len(read) == 2:
                both.set()
            await both.wait()
        return out
    monkeypatch.setattr(AsyncSession, "execute", execute)
    refresh = lambda: client.post("/api/auth/refresh", json={"refresh_token": token})  # noqa: E731
    rs = await asyncio.wait_for(asyncio.gather(refresh(), refresh()), 30)
    assert len(read) == 2 and sorted(r.status_code for r in rs) == [200, 401]


async def test_refresh_still_rotates(client, db, org):
    u, token = await _refresh_token(db, org)
    r = await client.post("/api/auth/refresh", json={"refresh_token": token})
    assert r.status_code == 200
    new = r.json()["refresh_token"]
    assert (await client.post("/api/auth/refresh", json={"refresh_token": token})).status_code == 401
    assert (await client.post("/api/auth/refresh", json={"refresh_token": new})).status_code == 200
    headers = {"Authorization": f"Bearer {make_access_token(u.id, org.id, 'admin')}"}
    assert (await client.get("/api/auth/me", headers=headers)).status_code == 200
