from datetime import datetime, timedelta
from sqlalchemy import select
import numpy as np

from app.celery_app import celery
from app.models import Agent, Incident, Trace
from app.tasks._sync_db import SyncSession


@celery.task(name="app.tasks.anomaly_tasks.detect_anomalies")
def detect_anomalies() -> dict:
    summary = {"checked": 0, "flagged": 0}
    with SyncSession() as db:
        agents = db.execute(select(Agent)).scalars().all()
        for agent in agents:
            summary["checked"] += 1
            now = datetime.utcnow()
            baseline_cutoff = now - timedelta(hours=6)
            rows = db.execute(
                select(Trace.status, Trace.created_at).where(
                    Trace.agent_id == agent.id,
                    Trace.created_at >= baseline_cutoff,
                )
            ).all()
            if len(rows) < 30:
                continue
            buckets: dict = {}
            for status, ts in rows:
                key = ts.replace(second=0, microsecond=0)
                b = buckets.setdefault(key, [0, 0])
                b[0] += 1
                if status != "success":
                    b[1] += 1
            series = [b for _, b in sorted(buckets.items())]
            rates = [(b[1] / b[0]) if b[0] else 0.0 for b in series]
            if len(rates) < 10:
                continue
            base = rates[:-15] if len(rates) > 30 else rates[: len(rates) // 2]
            recent = rates[-15:]
            base_mean = float(np.mean(base)) if base else 0.0
            base_std = float(np.std(base)) if base else 0.0
            recent_mean = float(np.mean(recent))
            threshold = base_mean + 2 * (base_std if base_std > 0 else 0.01)
            if recent_mean > threshold and recent_mean > 0.02:
                summary["flagged"] += 1
                inc = Incident(
                    org_id=agent.org_id,
                    agent_id=agent.id,
                    title=f"Error-rate anomaly on {agent.name}",
                    severity="warning",
                    timeline=[{
                        "ts": now.isoformat(),
                        "event": "anomaly_detected",
                        "detail": f"recent={recent_mean:.3f} threshold={threshold:.3f}",
                    }],
                    narrative=(
                        f"Detected anomalous error rate on agent {agent.name}: "
                        f"recent 15m mean {recent_mean:.2%} vs baseline {base_mean:.2%}"
                        f" (>2 std dev)."
                    ),
                )
                db.add(inc)
        db.commit()
    return summary
