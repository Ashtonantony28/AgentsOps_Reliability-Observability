import time

import pytest
from httpx import ASGITransport, AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import func, select

from app.config import settings
from app.models import Organization
from app.services.auth_service import hash_password, verify_password


def test_password_roundtrip():
    h = hash_password("S3cret!!")
    assert verify_password("S3cret!!", h)
    assert not verify_password("wrong", h)


async def _register(client, i):
    return await client.post("/api/auth/register", json={"email": f"bound-{i}@example.com", "password": "password123"})


@pytest.fixture
def clock(monkeypatch):
    now = [7_200_000.0]  # the start of an hour window
    monkeypatch.setattr(time, "time", lambda: now[0])
    return now


async def test_registrations_are_bounded_per_client(client, app_, clock):
    codes = [(await _register(client, i)).status_code for i in range(settings.REGISTER_LIMIT_PER_HOUR + 1)]
    assert codes == [200] * settings.REGISTER_LIMIT_PER_HOUR + [429]
    r = await _register(client, "late")
    assert r.status_code == 429 and r.json()["error"]["message"] == "rate_limited"
    async with AsyncClient(transport=ASGITransport(app=app_, client=("10.0.0.2", 1234)), base_url="http://test") as other:
        assert (await _register(other, "other")).status_code == 200
    clock[0] += 3599
    assert (await _register(client, "same-hour")).status_code == 429
    clock[0] += 1
    assert (await _register(client, "next-hour")).status_code == 200


async def test_registration_bound_is_configurable(client, clock, monkeypatch):
    monkeypatch.setattr(settings, "REGISTER_LIMIT_PER_HOUR", 1)
    assert [(await _register(client, i)).status_code for i in range(2)] == [200, 429]
    monkeypatch.setattr(settings, "REGISTER_LIMIT_PER_HOUR", 0)  # 0 turns it off, as for an org's limit
    assert (await _register(client, 2)).status_code == 200


async def test_registration_without_redis_is_refused(app_, db, redis, monkeypatch):
    async def down(*args, **kwargs):
        raise RedisConnectionError("redis down")

    monkeypatch.setattr(redis, "incr", down)  # as for every rate limit: no Redis, no request
    transport = ASGITransport(app=app_, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await _register(c, 0)
    assert r.status_code == 500 and r.json()["error"]["code"] == "internal_error"
    assert (await db.execute(select(func.count()).select_from(Organization))).scalar() == 0
