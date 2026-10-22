import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.celery_app import celery
from app.config import settings
from app.services.alert_service import evaluate_metric_rules


@celery.task(name="app.tasks.alert_tasks.evaluate_alert_rules")
def evaluate_alert_rules() -> dict:
    return asyncio.run(_evaluate())


async def _evaluate() -> dict:
    engine = create_async_engine(settings.db_url, pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as db:
            return await evaluate_metric_rules(db)
    finally:
        await engine.dispose()
