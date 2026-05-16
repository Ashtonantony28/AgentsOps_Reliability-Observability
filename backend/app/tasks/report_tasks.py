import asyncio
from datetime import datetime, timedelta

from sqlalchemy import select

from app.celery_app import celery
from app.models import Organization, OrgMembership, User
from app.services.notification_service import send_email
from app.tasks._sync_db import SyncSession


def _digest_text(org_name: str, agents_summary: list) -> str:
    lines = [f"AgentOps weekly digest for {org_name}", ""]
    lines.append("Top agents by cost (7d):")
    for a in agents_summary[:5]:
        lines.append(f"  - {a['agent_name']}: ${a['cost_usd']:.2f}, errors {a['error_rate']*100:.1f}%")
    lines.append("")
    lines.append("Open the dashboard for full details.")
    return "\n".join(lines)


@celery.task(name="app.tasks.report_tasks.send_weekly_digest")
def send_weekly_digest() -> dict:
    sent = 0
    with SyncSession() as db:
        orgs = db.execute(select(Organization)).scalars().all()
        for org in orgs:
            from app.models import Agent, Trace
            agents = db.execute(select(Agent).where(Agent.org_id == org.id)).scalars().all()
            summaries = []
            cutoff = datetime.utcnow() - timedelta(days=7)
            for a in agents:
                rows = db.execute(
                    select(Trace).where(Trace.agent_id == a.id, Trace.created_at >= cutoff)
                ).scalars().all()
                total = len(rows)
                errors = sum(1 for r in rows if r.status != "success")
                cost = float(sum(r.cost_usd for r in rows))
                summaries.append({
                    "agent_name": a.name,
                    "cost_usd": cost,
                    "error_rate": (errors / total) if total else 0.0,
                })
            summaries.sort(key=lambda x: x["cost_usd"], reverse=True)
            owners = db.execute(
                select(User).join(OrgMembership, OrgMembership.user_id == User.id)
                .where(OrgMembership.org_id == org.id, OrgMembership.role.in_(["owner", "admin"]))
            ).scalars().all()
            body = _digest_text(org.name, summaries)
            for u in owners:
                try:
                    asyncio.run(send_email(u.email, f"AgentOps weekly digest – {org.name}", body))
                    sent += 1
                except RuntimeError:
                    pass
    return {"sent": sent}
