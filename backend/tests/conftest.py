import os
import sys
import uuid

import fakeredis.aioredis
import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.database
import app.redis
import app.routers.ws
import app.services.trace_service
from app.config import settings
from app.database import Base, get_db
from app.main import app as fastapi_app
from app.middleware.auth import hash_api_key, make_access_token
from app.models import Agent, ApiKey, Organization, OrgMembership, User


@pytest.fixture
def sample_text():
    return "Contact me at jane.doe@example.com or 555-123-4567"


PG_TEST_DB = "agentops_test"


def pytest_configure(config):
    config.addinivalue_line("markers", "postgres: runs on Postgres; skipped where none is reachable, except in CI")


@pytest.fixture(scope="session")
def pg_url():
    """A fresh Postgres database with the current schema, created once per session."""
    host = os.environ.get("POSTGRES_HOST", "localhost")
    url = sa.engine.URL.create("postgresql+psycopg2", username=settings.POSTGRES_USER,
                               password=settings.POSTGRES_PASSWORD, host=host, port=settings.POSTGRES_PORT)
    admin = sa.create_engine(url.set(database=settings.POSTGRES_DB), isolation_level="AUTOCOMMIT",
                             connect_args={"connect_timeout": 2})
    try:
        with admin.connect() as c:
            c.execute(sa.text(f"DROP DATABASE IF EXISTS {PG_TEST_DB} WITH (FORCE)"))
            c.execute(sa.text(f"CREATE DATABASE {PG_TEST_DB}"))
    except sa.exc.OperationalError as e:
        admin.dispose()
        if os.environ.get("CI"):
            raise
        pytest.skip(f"postgres not reachable at {host}: {e.orig}")
    sync = sa.create_engine(url.set(database=PG_TEST_DB))
    Base.metadata.create_all(sync)
    sync.dispose()
    yield url.set(drivername="postgresql+asyncpg", database=PG_TEST_DB)
    with admin.connect() as c:
        c.execute(sa.text(f"DROP DATABASE IF EXISTS {PG_TEST_DB} WITH (FORCE)"))
    admin.dispose()


@pytest.fixture
async def engine(request, tmp_path):
    # TEST_DB=postgres runs every database test on Postgres; the `postgres` marker runs one test there.
    if os.environ.get("TEST_DB") == "postgres" or request.node.get_closest_marker("postgres"):
        # no pool: the websocket tests' sessions run on another thread's event loop
        eng = create_async_engine(request.getfixturevalue("pg_url"), poolclass=NullPool)
        async with eng.begin() as conn:
            tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
            await conn.execute(sa.text(f"TRUNCATE {tables} CASCADE"))
    else:
        # A file database with a connection per session: a session sees only what others have committed.
        eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db", connect_args={"timeout": 30})
        sa.event.listen(eng.sync_engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys = ON"))
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
def session_maker(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
async def db(session_maker):
    async with session_maker() as session:
        yield session


@pytest.fixture
def redis(monkeypatch):
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    original = app.redis.get_redis
    monkeypatch.setattr(app.redis, "_redis", fake)
    for name, mod in list(sys.modules.items()):
        if (name == "app" or name.startswith("app.")) and getattr(mod, "get_redis", None) is original:
            monkeypatch.setattr(mod, "get_redis", lambda: fake)
    return fake


@pytest.fixture
def enqueued(monkeypatch):
    """Trace ids queued for the batch policy check, instead of sending them to Celery."""
    ids: list[str] = []

    async def _enqueue(trace_id):
        ids.append(trace_id)

    monkeypatch.setattr(app.services.trace_service, "enqueue_trace_check", _enqueue)
    return ids


@pytest.fixture
def app_(session_maker, redis, enqueued, monkeypatch):
    async def _get_db():
        async with session_maker() as session:
            yield session

    monkeypatch.setattr(app.database, "async_session_maker", session_maker)
    monkeypatch.setattr(app.routers.ws, "async_session_maker", session_maker)
    fastapi_app.dependency_overrides[get_db] = _get_db
    yield fastapi_app
    fastapi_app.dependency_overrides.clear()


@pytest.fixture
async def client(app_):
    async with AsyncClient(transport=ASGITransport(app=app_), base_url="http://test") as c:
        yield c


@pytest.fixture
async def org(db):
    o = Organization(name="Test Org", slug=f"test-{uuid.uuid4().hex[:8]}")
    db.add(o)
    await db.commit()
    return o


async def _member(db, org, role: str) -> User:
    u = User(email=f"{role}-{uuid.uuid4().hex[:8]}@example.com", password_hash="x", full_name=role)
    db.add(u)
    await db.flush()
    db.add(OrgMembership(org_id=org.id, user_id=u.id, role=role))
    await db.commit()
    return u


@pytest.fixture
async def admin_user(db, org):
    return await _member(db, org, "admin")


@pytest.fixture
async def viewer_user(db, org):
    return await _member(db, org, "viewer")


@pytest.fixture
def auth_headers(org, admin_user):
    return {"Authorization": f"Bearer {make_access_token(admin_user.id, org.id, 'admin')}"}


@pytest.fixture
def viewer_headers(org, viewer_user):
    return {"Authorization": f"Bearer {make_access_token(viewer_user.id, org.id, 'viewer')}"}


@pytest.fixture
async def agent(db, org):
    a = Agent(org_id=org.id, name="test-agent")
    db.add(a)
    await db.commit()
    return a


@pytest.fixture
async def api_key(db, org):
    raw = f"ak_test_{uuid.uuid4().hex}"
    db.add(ApiKey(org_id=org.id, name="test", key_prefix=raw[:8], key_hash=hash_api_key(raw)))
    await db.commit()
    return raw


@pytest.fixture
def api_key_headers(api_key):
    return {"X-API-Key": api_key}
