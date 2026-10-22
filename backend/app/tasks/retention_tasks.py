import asyncio
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.celery_app import celery
from app.config import settings
from app.models import Organization, Trace


async def purge_expired_traces(db: AsyncSession) -> int:
    """Delete each org's traces older than its `retention_days` (0 or less keeps them); spans and violations go with
    them (ON DELETE CASCADE)."""
    now, deleted = datetime.utcnow(), 0
    orgs = (await db.execute(select(Organization.id, Organization.retention_days))).all()
    for org_id, days in orgs:
        if days and days > 0:
            r = await db.execute(delete(Trace).where(
                Trace.org_id == org_id, Trace.created_at < now - timedelta(days=days)))
            deleted += r.rowcount
            await db.commit()
    return deleted


@celery.task(name="app.tasks.retention_tasks.enforce_retention")
def enforce_retention() -> int:
    return asyncio.run(_enforce())


async def _enforce() -> int:
    engine = create_async_engine(settings.db_url, pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as db:
            return await purge_expired_traces(db)
    finally:
        await engine.dispose()
