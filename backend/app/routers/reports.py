from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import AuthContext, get_current_user
from app.models import Trace
from app.services.report_service import org_summary, summary_to_pdf, traces_to_csv

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.get("/summary")
async def summary(ctx: AuthContext = Depends(get_current_user), db: AsyncSession = Depends(get_db),
                  days: int = Query(7, ge=1, le=90)):
    return await org_summary(db, ctx.org_id, days=days)


@router.get("/summary.pdf")
async def summary_pdf(ctx: AuthContext = Depends(get_current_user), db: AsyncSession = Depends(get_db),
                      days: int = Query(7, ge=1, le=90)):
    data = await org_summary(db, ctx.org_id, days=days)
    pdf = summary_to_pdf(data)
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": "attachment; filename=summary.pdf"})


@router.get("/traces.csv")
async def traces_csv(ctx: AuthContext = Depends(get_current_user), db: AsyncSession = Depends(get_db),
                     hours: int = Query(24, ge=1, le=24 * 30)):
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    rows = (await db.execute(
        select(Trace).where(Trace.org_id == ctx.org_id, Trace.created_at >= cutoff)
        .order_by(Trace.created_at.desc()).limit(20000)
    )).scalars().all()
    return Response(traces_to_csv(rows), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=traces.csv"})


@router.get("/violations.csv")
async def violations_csv(ctx: AuthContext = Depends(get_current_user), db: AsyncSession = Depends(get_db),
                         days: int = Query(7, ge=1, le=90)):
    raise HTTPException(501, "not_implemented")
