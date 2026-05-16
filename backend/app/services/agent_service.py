from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, func, and_, case
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agent, AgentVersion, Trace


async def compute_agent_health(db: AsyncSession, agent_id: str) -> dict:
    cutoff = datetime.utcnow() - timedelta(hours=1)
    q = select(
        func.count(Trace.id),
        func.sum(case((Trace.status != "success", 1), else_=0)),
        func.avg(Trace.latency_ms),
        func.coalesce(func.sum(Trace.cost_usd), 0.0),
    ).where(Trace.agent_id == agent_id, Trace.created_at >= cutoff)
    total, errors, avg_latency, total_cost = (await db.execute(q)).one()

    total = total or 0
    errors = errors or 0
    error_rate = (errors / total) if total > 0 else 0.0

    if total == 0:
        health = "unknown"
    elif error_rate > 0.2 or (avg_latency or 0) > 10000:
        health = "down"
    elif error_rate > 0.05 or (avg_latency or 0) > 3000:
        health = "degraded"
    else:
        health = "healthy"

    cutoff24 = datetime.utcnow() - timedelta(hours=24)
    cost24 = (await db.execute(
        select(func.coalesce(func.sum(Trace.cost_usd), 0.0)).where(
            Trace.agent_id == agent_id, Trace.created_at >= cutoff24
        )
    )).scalar_one()

    # p95 latency via numpy on a window
    latencies = (await db.execute(
        select(Trace.latency_ms).where(
            Trace.agent_id == agent_id, Trace.created_at >= cutoff24
        )
    )).scalars().all()
    p95 = 0
    if latencies:
        import numpy as np
        p95 = int(np.percentile(latencies, 95))

    return {
        "health": health,
        "error_rate": round(error_rate, 4),
        "p95_latency_ms": p95,
        "cost_24h": round(float(cost24 or 0.0), 4),
    }


async def ensure_default_version(db: AsyncSession, agent: Agent) -> AgentVersion:
    existing = (await db.execute(
        select(AgentVersion).where(
            AgentVersion.agent_id == agent.id,
            AgentVersion.version == agent.current_version,
        )
    )).scalar_one_or_none()
    if existing:
        return existing
    v = AgentVersion(
        agent_id=agent.id,
        version=agent.current_version,
        model_name=agent.model_name,
        environment=agent.environment,
    )
    db.add(v)
    await db.commit()
    return v
