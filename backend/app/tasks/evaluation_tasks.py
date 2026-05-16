import asyncio

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.celery_app import celery
from app.config import settings
from app.models import EvaluationRun, EvaluationSet
from app.services.evaluation_service import run_evaluation
from app.tasks._sync_db import SyncSession


def _async_run(run_id: str) -> None:
    """Run async evaluation in its own event loop."""
    engine = create_async_engine(settings.db_url, pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def _work():
        async with Session() as db:
            await run_evaluation(db, run_id)
        await engine.dispose()

    asyncio.run(_work())


@celery.task(name="app.tasks.evaluation_tasks.execute_run")
def execute_run(run_id: str) -> str:
    _async_run(run_id)
    return run_id


@celery.task(name="app.tasks.evaluation_tasks.run_scheduled_evaluations")
def run_scheduled_evaluations() -> dict:
    """Look for eval sets with schedule_cron; trigger runs against agents tagged with them.
    For simplicity, treat schedule_cron as 'every-5-minutes' marker."""
    spawned = 0
    with SyncSession() as db:
        sets = db.execute(select(EvaluationSet).where(EvaluationSet.schedule_cron.isnot(None))).scalars().all()
        for s in sets:
            from app.models import Agent
            agents = db.execute(select(Agent).where(Agent.org_id == s.org_id)).scalars().all()
            for a in agents[:1]:
                run = EvaluationRun(set_id=s.id, agent_id=a.id, agent_version=a.current_version,
                                    triggered_by="scheduled")
                db.add(run)
                db.flush()
                spawned += 1
                execute_run.delay(run.id)
        db.commit()
    return {"spawned": spawned}
