import asyncio
import logging

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.celery_app import celery
from app.config import settings
from app.redis import close_redis
from app.services.violation_service import check_and_publish, drain_notifications

log = logging.getLogger(__name__)


# the counterexample search is bounded well inside the soft limit; past it the verdicts are stored without it
@celery.task(name="app.tasks.policy_tasks.check_completed_trace", acks_late=True, soft_time_limit=240, time_limit=300)
def check_completed_trace(trace_id: str) -> int:
    try:
        return asyncio.run(_check(trace_id, True))
    except SoftTimeLimitExceeded:
        log.error("policy check of trace %s hit its soft time limit; storing verdicts without counterexamples",
                  trace_id)
        return asyncio.run(_check(trace_id, False))


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


async def enqueue_trace_check(trace_id: str) -> None:
    try:
        await asyncio.to_thread(check_completed_trace.delay, trace_id)
    except Exception:
        log.exception("could not enqueue policy check for trace %s", trace_id)
