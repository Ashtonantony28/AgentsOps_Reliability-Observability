from datetime import datetime, timedelta
from typing import List

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agent, Incident, Trace


async def detect_error_rate_anomaly(db: AsyncSession, agent: Agent) -> dict | None:
    """Sliding window: compare last 15min error rate vs prior 6h baseline (std dev)."""
    now = datetime.utcnow()
    recent_cutoff = now - timedelta(minutes=15)
    baseline_cutoff = now - timedelta(hours=6)

    rows = (await db.execute(
        select(Trace.status, Trace.created_at).where(
            Trace.agent_id == agent.id,
            Trace.created_at >= baseline_cutoff,
        )
    )).all()
    if len(rows) < 30:
        return None

    # Build per-minute error rates
    buckets: dict = {}
    for status, ts in rows:
        bucket = ts.replace(second=0, microsecond=0)
        b = buckets.setdefault(bucket, [0, 0])
        b[0] += 1
        if status != "success":
            b[1] += 1
    if not buckets:
        return None

    series = sorted(buckets.items())
    rates = [(b[1] / b[0]) if b[0] else 0.0 for _, b in series]
    if len(rates) < 10:
        return None

    baseline = rates[:-15] if len(rates) > 30 else rates[: len(rates) // 2]
    recent = rates[-15:]

    base_mean = float(np.mean(baseline)) if baseline else 0.0
    base_std = float(np.std(baseline)) if baseline else 0.0
    recent_mean = float(np.mean(recent)) if recent else 0.0
    threshold = base_mean + 2 * (base_std if base_std > 0 else 0.01)

    if recent_mean > threshold and recent_mean > 0.02:
        return {
            "agent_id": agent.id,
            "baseline_mean": round(base_mean, 4),
            "baseline_std": round(base_std, 4),
            "recent_mean": round(recent_mean, 4),
            "threshold": round(threshold, 4),
        }
    return None
