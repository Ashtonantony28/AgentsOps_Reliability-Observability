import asyncio
import logging

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.celery_app import celery
from app.config import settings
from app.redis import close_redis
from app.services.trace_policy_service import TraceBusy
from app.services.violation_service import check_and_publish, drain_notifications

log = logging.getLogger(__name__)


# the counterexample search is bounded well inside the soft limit; past it the verdicts are stored without it
@celery.task(bind=True, name="app.tasks.policy_tasks.check_completed_trace", acks_late=True, soft_time_limit=240,
             time_limit=300, max_retries=5)
def check_completed_trace(self, trace_id: str) -> int:
    try:
        try:
            return asyncio.run(_check(trace_id, True))
        except SoftTimeLimitExceeded:
            log.error("policy check of trace %s hit its soft time limit; storing verdicts without counterexamples",
                      trace_id)
            return asyncio.run(_check(trace_id, False))
    except TraceBusy as e:
        raise self.retry(exc=e, countdown=60)


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


async def enqueue_trace_check(trace_id: str) -> None:
    try:
        await asyncio.to_thread(check_completed_trace.delay, trace_id)
    except Exception:
        log.exception("could not enqueue policy check for trace %s", trace_id)
