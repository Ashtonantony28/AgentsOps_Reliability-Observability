import csv
import io
import json
from datetime import datetime, timedelta
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import AuthContext, get_current_user, require_role
from app.models import Agent, AuditLog, PolicyRule, Span, Trace
from app.schemas.integration import AuditLogOut, PolicyRuleCreate, PolicyRuleOut

router = APIRouter(prefix="/api/governance", tags=["governance"])


@router.get("/audit-log", response_model=List[AuditLogOut])
async def list_audit_log(
    ctx: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    days: int = Query(30, ge=1, le=365),
    limit: int = 500,
):
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = (await db.execute(
        select(AuditLog).where(AuditLog.org_id == ctx.org_id, AuditLog.created_at >= cutoff)
        .order_by(AuditLog.created_at.desc()).limit(limit)
    )).scalars().all()
    return [AuditLogOut.model_validate(r) for r in rows]


@router.get("/audit-log/export.csv")
async def export_audit_csv(ctx: AuthContext = Depends(get_current_user), db: AsyncSession = Depends(get_db),
                            days: int = Query(30, ge=1, le=365)):
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = (await db.execute(
        select(AuditLog).where(AuditLog.org_id == ctx.org_id, AuditLog.created_at >= cutoff)
        .order_by(AuditLog.created_at.desc())
    )).scalars().all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "actor_type", "actor_id", "action", "resource_type", "resource_id", "rationale", "created_at"])
    for r in rows:
        w.writerow([r.id, r.actor_type, r.actor_id, r.action, r.resource_type, r.resource_id, r.rationale, r.created_at.isoformat()])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=audit.csv"})


@router.get("/audit-log/export.json")
async def export_audit_json(ctx: AuthContext = Depends(get_current_user), db: AsyncSession = Depends(get_db),
                             days: int = Query(30, ge=1, le=365)):
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = (await db.execute(
        select(AuditLog).where(AuditLog.org_id == ctx.org_id, AuditLog.created_at >= cutoff)
        .order_by(AuditLog.created_at.desc())
    )).scalars().all()
    data = [
        {
            "id": r.id, "actor_type": r.actor_type, "actor_id": r.actor_id,
            "action": r.action, "resource_type": r.resource_type, "resource_id": r.resource_id,
            "rationale": r.rationale, "details": r.details,
            "created_at": r.created_at.isoformat(),
        } for r in rows
    ]
    return Response(json.dumps(data, indent=2), media_type="application/json",
                    headers={"Content-Disposition": "attachment; filename=audit.json"})


@router.get("/policies", response_model=List[PolicyRuleOut])
async def list_policies(ctx: AuthContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(PolicyRule).where(PolicyRule.org_id == ctx.org_id)
    )).scalars().all()
    return [PolicyRuleOut.model_validate(r) for r in rows]


@router.post("/policies", response_model=PolicyRuleOut)
async def create_policy(
    req: PolicyRuleCreate,
    ctx: AuthContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    p = PolicyRule(
        org_id=ctx.org_id, name=req.name, description=req.description,
        rule=req.rule, action=req.action, enabled=req.enabled,
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    db.add(AuditLog(
        org_id=ctx.org_id, actor_type="user", actor_id=ctx.user.id,
        action="policy_create", resource_type="policy", resource_id=p.id,
        details={"name": req.name, "action": req.action},
    ))
    await db.commit()
    return PolicyRuleOut.model_validate(p)


@router.delete("/policies/{policy_id}")
async def delete_policy(policy_id: str, ctx: AuthContext = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    p = (await db.execute(select(PolicyRule).where(PolicyRule.id == policy_id, PolicyRule.org_id == ctx.org_id))).scalar_one_or_none()
    if not p:
        raise HTTPException(404, "policy_not_found")
    await db.delete(p)
    await db.commit()
    return {"ok": True}


@router.get("/least-privilege")
async def least_privilege(ctx: AuthContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Show tools actually used vs tools allowed per agent (last 7d)."""
    cutoff = datetime.utcnow() - timedelta(days=7)
    agents = (await db.execute(select(Agent).where(Agent.org_id == ctx.org_id))).scalars().all()
    results = []
    for a in agents:
        used = set()
        traces = (await db.execute(
            select(Trace.id).where(Trace.agent_id == a.id, Trace.created_at >= cutoff)
        )).scalars().all()
        if traces:
            spans = (await db.execute(
                select(Span.name).where(Span.trace_id.in_(traces), Span.kind == "tool")
            )).scalars().all()
            used = set(spans)
        allowed = set(a.allowed_tools or [])
        unused = list(allowed - used)
        undeclared = list(used - allowed)
        results.append({
            "agent_id": a.id,
            "agent_name": a.name,
            "allowed_tools": list(allowed),
            "used_tools": list(used),
            "unused_permissions": unused,
            "undeclared_usage": undeclared,
            "recommendation": "drop " + ", ".join(unused) if unused else "ok",
        })
    return {"agents": results}


@router.get("/scorecard")
async def scorecard(ctx: AuthContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Org-wide health scorecard: pii hits, injection attempts, policy blocks last 30d."""
    cutoff = datetime.utcnow() - timedelta(days=30)
    pii_hits = (await db.execute(
        select(func.count(Trace.id)).where(
            Trace.org_id == ctx.org_id, Trace.created_at >= cutoff,
            func.json_array_length(func.cast(Trace.pii_flags, type_=func.coalesce(Trace.pii_flags, []).type)) > 0
        )
    )).scalar_one() if False else 0  # SQLAlchemy/pg func varies; do it manually below
    # manual count
    rows = (await db.execute(
        select(Trace.pii_flags, Trace.injection_score).where(
            Trace.org_id == ctx.org_id, Trace.created_at >= cutoff
        )
    )).all()
    pii_hits = sum(1 for f, _ in rows if f)
    injection_hits = sum(1 for _, s in rows if (s or 0) > 0.3)

    policy_blocks = (await db.execute(
        select(func.count(AuditLog.id)).where(
            AuditLog.org_id == ctx.org_id,
            AuditLog.action == "policy_block",
            AuditLog.created_at >= cutoff,
        )
    )).scalar_one() or 0

    return {
        "pii_flags_30d": pii_hits,
        "injection_attempts_30d": injection_hits,
        "policy_blocks_30d": policy_blocks,
        "total_traces_30d": len(rows),
    }
