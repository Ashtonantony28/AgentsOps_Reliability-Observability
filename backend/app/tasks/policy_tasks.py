import asyncio
import logging

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.celery_app import celery
from app.config import settings
from app.redis import close_redis, get_redis
from app.services.trace_policy_service import TraceBusy
from app.services.violation_service import check_and_publish, drain_notifications

log = logging.getLogger(__name__)
# Each queued task checks the next trace in turn by org, not its own: an org's backlog of checks delays another org's
# by at most one of its own per worker
TURN_KEY = "policy:batch:orgs"  # orgs with traces waiting, in turn order
_PUSH = """
redis.call('rpush', KEYS[2], ARGV[1])
if redis.call('llen', KEYS[2]) == 1 then redis.call('rpush', KEYS[1], ARGV[2]) end
"""
_POP = """
local org = redis.call('lpop', KEYS[1])
if not org then return false end
local key = ARGV[1] .. org
local trace_id = redis.call('lpop', key)
if redis.call('llen', key) > 0 then redis.call('rpush', KEYS[1], org) end
return trace_id
"""


def _waiting_key(org_id: str) -> str:
    return f"policy:batch:org:{org_id}"


# the counterexample search is bounded well inside the soft limit; past it the verdicts are stored without it
@celery.task(bind=True, name="app.tasks.policy_tasks.check_completed_trace", acks_late=True, soft_time_limit=240,
             time_limit=300, max_retries=5)
def check_completed_trace(self, trace_id: str | None = None) -> int:
    if trace_id is None and (trace_id := asyncio.run(_next())) is None:
        return 0
    try:
        try:
            return asyncio.run(_check(trace_id, True))
        except SoftTimeLimitExceeded:
            log.error("policy check of trace %s hit its soft time limit; storing verdicts without counterexamples",
                      trace_id)
            return asyncio.run(_check(trace_id, False))
    except TraceBusy as e:
        raise self.retry(exc=e, args=(trace_id,), countdown=60)


async def _check(trace_id: str, counterexamples: bool) -> int:
    engine = create_async_engine(settings.db_url, pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as db:
            n = len(await check_and_publish(db, trace_id, counterexamples))
        await drain_notifications()
        return n
    finally:
        await close_redis()  # the client is bound to this event loop
        await engine.dispose()


@celery.task(name="app.tasks.policy_tasks.close_idle_streams")
def close_idle_streams() -> int:
    return asyncio.run(_close_idle())


async def _close_idle() -> int:
    from app.services.trace_service import close_idle_streams as close  # imports this module

    engine = create_async_engine(settings.db_url, pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as db:
            return len(await close(db))
    finally:
        await close_redis()
        await engine.dispose()


async def next_trace_check() -> str | None:
    return await get_redis().eval(_POP, 1, TURN_KEY, _waiting_key(""))


async def _next() -> str | None:
    try:
        return await next_trace_check()
    finally:
        await close_redis()


async def enqueue_trace_check(trace_id: str, org_id: str) -> None:
    args: tuple = ()
    try:
        await get_redis().eval(_PUSH, 2, TURN_KEY, _waiting_key(org_id), trace_id, org_id)
    except Exception:  # checked first come first served instead
        log.exception("could not queue policy check for trace %s in turn", trace_id)
        args = (trace_id,)
    try:
        await asyncio.to_thread(check_completed_trace.delay, *args)
    except Exception:
        log.exception("could not enqueue policy check for trace %s", trace_id)
