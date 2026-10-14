from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import AuthContext, get_current_user, require_role
from app.models import AlertEvent, AlertRule
from app.schemas.alert import (
    AlertEventOut,
    AlertRuleCreate,
    AlertRuleOut,
    AlertRuleUpdate,
)

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


@router.get("/rules", response_model=List[AlertRuleOut])
async def list_rules(ctx: AuthContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(AlertRule).where(AlertRule.org_id == ctx.org_id).order_by(AlertRule.created_at.desc())
    )).scalars().all()
    return [AlertRuleOut.model_validate(r) for r in rows]


@router.post("/rules", response_model=AlertRuleOut)
async def create_rule(
    req: AlertRuleCreate,
    ctx: AuthContext = Depends(require_role("developer")),
    db: AsyncSession = Depends(get_db),
):
    r = AlertRule(
        org_id=ctx.org_id,
        agent_id=req.agent_id,
        name=req.name,
        metric=req.metric,
        operator=req.operator,
        threshold=req.threshold,
        window_minutes=req.window_minutes,
        severity=req.severity,
        channels=[c.model_dump() for c in req.channels],
        runbook_url=req.runbook_url,
        dedup_minutes=req.dedup_minutes,
        enabled=req.enabled,
        context=req.context.model_dump(exclude_none=True),
    )
    db.add(r)
    await db.commit()
    await db.refresh(r)
    return AlertRuleOut.model_validate(r)


@router.patch("/rules/{rule_id}", response_model=AlertRuleOut)
async def update_rule(
    rule_id: str,
    req: AlertRuleUpdate,
    ctx: AuthContext = Depends(require_role("developer")),
    db: AsyncSession = Depends(get_db),
):
    r = (await db.execute(select(AlertRule).where(AlertRule.id == rule_id, AlertRule.org_id == ctx.org_id))).scalar_one_or_none()
    if not r:
        raise HTTPException(404, "rule_not_found")
    payload = req.model_dump(exclude_none=True)
    if "channels" in payload and payload["channels"] is not None:
        payload["channels"] = [c if isinstance(c, dict) else c.model_dump() for c in payload["channels"]]
    for k, v in payload.items():
        setattr(r, k, v)
    await db.commit()
    return AlertRuleOut.model_validate(r)


@router.delete("/rules/{rule_id}")
async def delete_rule(rule_id: str, ctx: AuthContext = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    r = (await db.execute(select(AlertRule).where(AlertRule.id == rule_id, AlertRule.org_id == ctx.org_id))).scalar_one_or_none()
    if not r:
        raise HTTPException(404, "rule_not_found")
    await db.delete(r)
    await db.commit()
    return {"ok": True}


@router.get("/events", response_model=List[AlertEventOut])
async def list_events(
    ctx: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    status: str | None = None,
):
    q = select(AlertEvent).where(AlertEvent.org_id == ctx.org_id)
    if status:
        q = q.where(AlertEvent.status == status)
    rows = (await db.execute(q.order_by(AlertEvent.fired_at.desc()).limit(200))).scalars().all()
    return [AlertEventOut.model_validate(r) for r in rows]


@router.post("/events/{event_id}/ack", response_model=AlertEventOut)
async def ack(event_id: str, ctx: AuthContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    e = (await db.execute(select(AlertEvent).where(AlertEvent.id == event_id, AlertEvent.org_id == ctx.org_id))).scalar_one_or_none()
    if not e:
        raise HTTPException(404, "event_not_found")
    e.status = "acknowledged"
    e.acknowledged_at = datetime.utcnow()
    e.assignee_id = ctx.user.id
    await db.commit()
    return AlertEventOut.model_validate(e)


@router.post("/events/{event_id}/resolve", response_model=AlertEventOut)
async def resolve(event_id: str, ctx: AuthContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    e = (await db.execute(select(AlertEvent).where(AlertEvent.id == event_id, AlertEvent.org_id == ctx.org_id))).scalar_one_or_none()
    if not e:
        raise HTTPException(404, "event_not_found")
    e.status = "resolved"
    e.resolved_at = datetime.utcnow()
    await db.commit()
    return AlertEventOut.model_validate(e)
