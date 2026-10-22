import asyncio
import logging
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.celery_app import celery
from app.config import settings
from app.models import Agent, EvaluationRun, EvaluationSet
from app.services import cron
from app.services.evaluation_service import run_evaluation
from app.tasks._sync_db import SyncSession

log = logging.getLogger(__name__)


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
    return {"spawned": schedule_due_runs(datetime.utcnow())}


def schedule_due_runs(now: datetime) -> int:
    """Start a run of each set whose `schedule_cron` fell due since its last scheduled run (or its creation), against
    the agent of the set's latest run at that agent's current version; a set never run has no agent and waits."""
    due = []
    with SyncSession() as db:
        sets = db.execute(select(EvaluationSet).where(EvaluationSet.schedule_cron.isnot(None))).scalars().all()
        for s in sets:
            try:
                expr = cron.parse(s.schedule_cron)
            except ValueError as e:
                log.warning("eval set %s: schedule_cron %r skipped: %s", s.id, s.schedule_cron, e)
                continue
            runs = select(EvaluationRun).where(EvaluationRun.set_id == s.id)
            last = db.execute(runs.where(EvaluationRun.triggered_by == "scheduled").with_only_columns(
                func.max(EvaluationRun.created_at))).scalar_one()
            if cron.due(expr, last or s.created_at, now) is None:
                continue
            latest = db.execute(runs.order_by(EvaluationRun.created_at.desc()).limit(1)).scalar_one_or_none()
            agent = latest and db.get(Agent, latest.agent_id)
            if agent is None:
                continue
            run = EvaluationRun(set_id=s.id, agent_id=agent.id, agent_version=agent.current_version,
                                triggered_by="scheduled", created_at=now)
            db.add(run)
            due.append(run)
        db.commit()  # before the worker looks the runs up
    for run in due:
        execute_run.delay(run.id)
    return len(due)
