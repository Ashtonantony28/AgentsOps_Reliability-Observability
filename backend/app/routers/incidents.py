from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import AuthContext, get_current_user
from app.models import Incident
from app.schemas.alert import IncidentOut
from app.services.alert_service import build_incident_narrative

router = APIRouter(prefix="/api/incidents", tags=["incidents"])


@router.get("", response_model=List[IncidentOut])
async def list_incidents(
    ctx: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    status: str | None = None,
):
    q = select(Incident).where(Incident.org_id == ctx.org_id)
    if status:
        q = q.where(Incident.status == status)
    rows = (await db.execute(q.order_by(Incident.started_at.desc()).limit(200))).scalars().all()
    return [IncidentOut.model_validate(r) for r in rows]


@router.get("/{incident_id}", response_model=IncidentOut)
async def get_incident(incident_id: str, ctx: AuthContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    inc = (await db.execute(
        select(Incident).where(Incident.id == incident_id, Incident.org_id == ctx.org_id)
    )).scalar_one_or_none()
    if not inc:
        raise HTTPException(404, "incident_not_found")
    if not inc.narrative:
        inc.narrative = await build_incident_narrative(db, inc)
        await db.commit()
    return IncidentOut.model_validate(inc)


@router.post("/{incident_id}/resolve", response_model=IncidentOut)
async def resolve(incident_id: str, ctx: AuthContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    inc = (await db.execute(
        select(Incident).where(Incident.id == incident_id, Incident.org_id == ctx.org_id)
    )).scalar_one_or_none()
    if not inc:
        raise HTTPException(404, "incident_not_found")
    inc.status = "resolved"
    inc.resolved_at = datetime.utcnow()
    inc.timeline = (inc.timeline or []) + [{
        "ts": datetime.utcnow().isoformat(),
        "event": "resolved",
        "detail": f"resolved by {ctx.user.email}",
    }]
    await db.commit()
    return IncidentOut.model_validate(inc)
