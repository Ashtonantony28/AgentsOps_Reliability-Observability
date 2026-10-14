import asyncio
import logging

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.celery_app import celery
from app.config import settings
from app.services.trace_policy_service import check_completed_trace as check_trace_policies

log = logging.getLogger(__name__)


@celery.task(name="app.tasks.policy_tasks.check_completed_trace", acks_late=True)
def check_completed_trace(trace_id: str) -> int:
    engine = create_async_engine(settings.db_url, pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def _work() -> int:
        try:
            async with Session() as db:
                return len(await check_trace_policies(db, trace_id))
        finally:
            await engine.dispose()

    return asyncio.run(_work())


async def enqueue_trace_check(trace_id: str) -> None:
    try:
        await asyncio.to_thread(check_completed_trace.delay, trace_id)
    except Exception:
        log.exception("could not enqueue policy check for trace %s", trace_id)
