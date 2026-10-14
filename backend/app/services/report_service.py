import csv
import io
from datetime import datetime, timedelta
from typing import Iterable, List

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agent, EvaluationRun, Trace, TracePolicyViolation
from app.services.alert_service import SEVERITIES

VIOLATION_CSV_COLUMNS = ["detected_at", "agent", "trace_id", "policy", "severity", "at_seq", "counterexample", "mode"]


async def violation_summary(db: AsyncSession, org_id: str, cutoff: datetime) -> dict:
    counts = (await db.execute(
        select(TracePolicyViolation.agent_id, TracePolicyViolation.policy_name, TracePolicyViolation.severity,
               func.count())
        .where(TracePolicyViolation.org_id == org_id, TracePolicyViolation.detected_at >= cutoff)
        .group_by(TracePolicyViolation.agent_id, TracePolicyViolation.policy_name, TracePolicyViolation.severity)
    )).all()
    by_severity = dict.fromkeys(reversed(SEVERITIES), 0)
    by_policy: dict[tuple[str, str], int] = {}
    by_agent: dict[str, int] = {}
    for agent_id, policy, severity, n in counts:
        by_severity[severity] = by_severity.get(severity, 0) + n
        by_policy[policy, severity] = by_policy.get((policy, severity), 0) + n
        by_agent[agent_id] = by_agent.get(agent_id, 0) + n
    rank = {s: i for i, s in enumerate(SEVERITIES)}
    return {
        "total": sum(by_severity.values()),
        "by_severity": by_severity,
        "by_policy": [{"policy_name": p, "severity": s, "count": n} for (p, s), n in
                      sorted(by_policy.items(), key=lambda kv: (-rank.get(kv[0][1], -1), -kv[1], kv[0][0]))],
        "by_agent": by_agent,
    }


async def org_summary(db: AsyncSession, org_id: str, days: int = 7) -> dict:
    cutoff = datetime.utcnow() - timedelta(days=days)
    agents = (await db.execute(select(Agent).where(Agent.org_id == org_id))).scalars().all()
    violations = await violation_summary(db, org_id, cutoff)
    by_agent = violations.pop("by_agent")
    summaries = []
    for a in agents:
        rows = (await db.execute(
            select(Trace).where(Trace.agent_id == a.id, Trace.created_at >= cutoff)
        )).scalars().all()
        total = len(rows)
        errors = sum(1 for r in rows if r.status != "success")
        cost = float(sum(r.cost_usd for r in rows))
        tokens = sum(r.tokens_input + r.tokens_output for r in rows)
        summaries.append({
            "agent_id": a.id,
            "agent_name": a.name,
            "traces": total,
            "errors": errors,
            "error_rate": round(errors / total, 4) if total else 0.0,
            "cost_usd": round(cost, 4),
            "tokens": tokens,
            "policy_violations": by_agent.get(a.id, 0),
        })

    last_eval_runs = (await db.execute(
        select(EvaluationRun).where(
            EvaluationRun.created_at >= cutoff,
        ).order_by(EvaluationRun.created_at.desc()).limit(20)
    )).scalars().all()

    top_cost = sorted(summaries, key=lambda x: x["cost_usd"], reverse=True)[:5]
    worst_err = sorted(summaries, key=lambda x: x["error_rate"], reverse=True)[:5]
    return {
        "window_days": days,
        "agents": summaries,
        "top_by_cost": top_cost,
        "worst_by_error_rate": worst_err,
        "policy_violations": violations,
        "recent_evals": [
            {
                "id": r.id, "agent_id": r.agent_id, "score": r.score,
                "passed": r.passed, "failed": r.failed,
                "created_at": r.created_at.isoformat(),
            } for r in last_eval_runs
        ],
    }


def traces_to_csv(rows: Iterable[Trace]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "agent_id", "status", "latency_ms", "tokens_in", "tokens_out",
                "cost_usd", "environment", "created_at"])
    for r in rows:
        w.writerow([r.id, r.agent_id, r.status, r.latency_ms, r.tokens_input,
                    r.tokens_output, r.cost_usd, r.environment, r.created_at.isoformat()])
    return buf.getvalue()


def violations_to_csv(rows: Iterable[TracePolicyViolation], agent_names: dict[str, str]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(VIOLATION_CSV_COLUMNS)
    for r in rows:
        w.writerow([r.detected_at.isoformat(), agent_names.get(r.agent_id, r.agent_id), r.trace_id, r.policy_name,
                    r.severity, r.at_seq, " ".join(map(str, r.counterexample or [])), r.mode])
    return buf.getvalue()


def summary_to_pdf(summary: dict) -> bytes:
    """Generate a tiny PDF executive summary using reportlab."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    width, height = letter
    y = height - 50
    c.setFont("Helvetica-Bold", 18)
    c.drawString(50, y, "AgentOps — Executive Summary")
    y -= 30
    c.setFont("Helvetica", 10)
    c.drawString(50, y, f"Window: last {summary.get('window_days')} days")
    y -= 20
    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, y, "Top agents by cost")
    y -= 16
    c.setFont("Helvetica", 10)
    for a in summary.get("top_by_cost", []):
        c.drawString(60, y, f"- {a['agent_name']}: ${a['cost_usd']:.2f} ({a['traces']} traces)")
        y -= 14
    y -= 8
    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, y, "Worst by error rate")
    y -= 16
    c.setFont("Helvetica", 10)
    for a in summary.get("worst_by_error_rate", []):
        c.drawString(60, y, f"- {a['agent_name']}: {a['error_rate']*100:.1f}% errors")
        y -= 14
    pv = summary.get("policy_violations") or {"total": 0, "by_severity": {}, "by_policy": []}
    y -= 8
    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, y, "Policy violations")
    y -= 16
    c.setFont("Helvetica", 10)
    sev = ", ".join(f"{n} {s}" for s, n in pv["by_severity"].items() if n)
    c.drawString(60, y, f"Total: {pv['total']}" + (f" ({sev})" if sev else ""))
    y -= 14
    for p in pv["by_policy"][:10]:
        c.drawString(60, y, f"- {p['policy_name']} [{p['severity']}]: {p['count']}")
        y -= 14
    c.save()
    return buf.getvalue()
