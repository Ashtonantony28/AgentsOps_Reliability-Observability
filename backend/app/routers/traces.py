from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
import numpy as np

from app.database import get_db
from app.middleware.auth import AuthContext, authenticate_api_key, get_current_user
from app.middleware.rate_limit import enforce_rate_limit
from app.models import Organization, Trace, Span, Agent
from app.schemas.policy import EventsAppendIn, EventsAppendOut, ViolationOut
from app.schemas.trace import (
    CostBurn,
    MetricsResponse,
    SpanOut,
    TimeSeriesPoint,
    TraceBulkIn,
    TraceDetailOut,
    TraceIn,
    TraceOut,
)
from app.services.report_service import traces_to_csv
from app.services.trace_service import PolicyViolation, TraceConflict, append_events as append_trace_events, ingest_trace

router = APIRouter(prefix="/api/traces", tags=["traces"])


# ---- Ingestion (API-key authed) ----
@router.post("/ingest", status_code=202)
async def ingest_one(req: TraceIn, request: Request, db: AsyncSession = Depends(get_db)):
    org, api_key = await authenticate_api_key(request, db)
    await enforce_rate_limit(f"org:{org.id}", org.rate_limit_per_min)
    try:
        trace = await ingest_trace(db, org.id, req)
    except PolicyViolation as e:
        raise HTTPException(403, detail={"error": "policy_block", "rule": e.rule_name, "message": str(e)})
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    return {"id": trace.id, "trace_uid": trace.trace_uid}


@router.post("/ingest/bulk", status_code=202)
async def ingest_bulk(req: TraceBulkIn, request: Request, db: AsyncSession = Depends(get_db)):
    org, api_key = await authenticate_api_key(request, db)
    await enforce_rate_limit(f"org:{org.id}", org.rate_limit_per_min)
    accepted, rejected = [], []
    for t in req.traces:
        try:
            tr = await ingest_trace(db, org.id, t)
            accepted.append({"id": tr.id, "trace_uid": tr.trace_uid})
        except PolicyViolation as e:
            rejected.append({"reason": "policy_block", "rule": e.rule_name})
        except ValueError as e:
            rejected.append({"reason": str(e)})
    return {"accepted": accepted, "rejected": rejected}


@router.post("/{trace_uid}/events", response_model=EventsAppendOut, status_code=202)
async def append_events(trace_uid: str, req: EventsAppendIn, request: Request, db: AsyncSession = Depends(get_db)):
    org, api_key = await authenticate_api_key(request, db)
    await enforce_rate_limit(f"org:{org.id}", org.rate_limit_per_min)
    try:
        _trace, rows = await append_trace_events(db, org.id, trace_uid, req)
    except PolicyViolation as e:
        raise HTTPException(403, detail={"error": "policy_block", "rule": e.rule_name, "message": str(e)})
    except TraceConflict as e:
        raise HTTPException(409, detail={"error": e.code, "message": e.message, **e.details})
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    return EventsAppendOut(accepted=len(req.events), violations=[ViolationOut.model_validate(r) for r in rows])


# ---- Query (JWT authed) ----
@router.get("", response_model=List[TraceOut])
async def list_traces(
    ctx: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    agent_id: Optional[str] = None,
    status: Optional[str] = None,
    environment: Optional[str] = None,
    limit: int = Query(100, le=500),
    offset: int = 0,
    since_hours: int = Query(24, ge=1, le=24 * 30),
):
    cutoff = datetime.utcnow() - timedelta(hours=since_hours)
    q = select(Trace).where(Trace.org_id == ctx.org_id, Trace.created_at >= cutoff)
    if agent_id:
        q = q.where(Trace.agent_id == agent_id)
    if status:
        q = q.where(Trace.status == status)
    if environment:
        q = q.where(Trace.environment == environment)
    q = q.order_by(Trace.created_at.desc()).offset(offset).limit(limit)
    rows = (await db.execute(q)).scalars().all()
    return [TraceOut.model_validate(r) for r in rows]


@router.get("/metrics", response_model=MetricsResponse)
async def metrics(
    ctx: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    agent_id: Optional[str] = None,
    hours: int = Query(24, ge=1, le=24 * 30),
):
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    bucket_size = max(60, hours * 3600 // 60)  # ~60 buckets

    q = select(Trace.status, Trace.latency_ms, Trace.tokens_input, Trace.tokens_output,
               Trace.cost_usd, Trace.created_at).where(
        Trace.org_id == ctx.org_id, Trace.created_at >= cutoff
    )
    if agent_id:
        q = q.where(Trace.agent_id == agent_id)
    rows = (await db.execute(q)).all()

    buckets: dict[datetime, dict] = {}
    for status, lat, ti, to, cost, ts in rows:
        bucket = datetime.utcfromtimestamp((int(ts.timestamp()) // bucket_size) * bucket_size)
        b = buckets.setdefault(bucket, {"total": 0, "errors": 0, "lat": [], "tokens": 0, "cost": 0.0})
        b["total"] += 1
        if status != "success":
            b["errors"] += 1
        b["lat"].append(lat or 0)
        b["tokens"] += (ti or 0) + (to or 0)
        b["cost"] += float(cost or 0.0)

    sorted_buckets = sorted(buckets.items())

    def ts_series(getter):
        return [TimeSeriesPoint(t=k, value=getter(v)) for k, v in sorted_buckets]

    success = ts_series(lambda v: round(1 - v["errors"] / v["total"], 4) if v["total"] else 1.0)
    error = ts_series(lambda v: round(v["errors"] / v["total"], 4) if v["total"] else 0.0)
    lat_avg = ts_series(lambda v: round(float(np.mean(v["lat"])), 1) if v["lat"] else 0.0)
    lat_p95 = ts_series(lambda v: round(float(np.percentile(v["lat"], 95)), 1) if v["lat"] else 0.0)
    tokens = ts_series(lambda v: float(v["tokens"]))
    costs = ts_series(lambda v: round(v["cost"], 4))

    total_traces = sum(b["total"] for b in buckets.values()) or 0
    total_errors = sum(b["errors"] for b in buckets.values()) or 0
    total_tokens = sum(b["tokens"] for b in buckets.values()) or 0
    total_cost = sum(b["cost"] for b in buckets.values()) or 0.0

    return MetricsResponse(
        success_rate=success,
        error_rate=error,
        latency_avg_ms=lat_avg,
        latency_p95_ms=lat_p95,
        tokens=tokens,
        cost_usd=costs,
        totals={
            "traces": total_traces,
            "errors": total_errors,
            "tokens": total_tokens,
            "cost_usd": round(total_cost, 4),
            "error_rate": round(total_errors / total_traces, 4) if total_traces else 0.0,
        },
    )


@router.get("/cost-burn", response_model=CostBurn)
async def cost_burn(
    ctx: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    days: int = Query(30, ge=1, le=90),
):
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = (await db.execute(
        select(Trace.created_at, Trace.cost_usd, Trace.agent_id).where(
            Trace.org_id == ctx.org_id, Trace.created_at >= cutoff
        )
    )).all()

    daily: dict = {}
    by_agent: dict = {}
    for ts, cost, agent_id in rows:
        day = ts.replace(hour=0, minute=0, second=0, microsecond=0)
        daily[day] = daily.get(day, 0.0) + float(cost or 0.0)
        by_agent[agent_id] = by_agent.get(agent_id, 0.0) + float(cost or 0.0)

    daily_points = [TimeSeriesPoint(t=k, value=round(v, 4)) for k, v in sorted(daily.items())]
    avg = (sum(daily.values()) / max(1, len(daily))) if daily else 0.0
    projection = round(avg * 30, 2)

    agents = (await db.execute(select(Agent).where(Agent.org_id == ctx.org_id))).scalars().all()
    name_map = {a.id: a.name for a in agents}
    by_agent_list = [
        {"agent_id": k, "agent_name": name_map.get(k, k), "cost_usd": round(v, 4)}
        for k, v in sorted(by_agent.items(), key=lambda kv: kv[1], reverse=True)
    ]
    return CostBurn(daily=daily_points, monthly_projection_usd=projection, by_agent=by_agent_list)


@router.get("/export.csv")
async def export_csv(
    ctx: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    agent_id: Optional[str] = None,
    hours: int = Query(24, ge=1, le=24 * 30),
):
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    q = select(Trace).where(Trace.org_id == ctx.org_id, Trace.created_at >= cutoff)
    if agent_id:
        q = q.where(Trace.agent_id == agent_id)
    rows = (await db.execute(q.order_by(Trace.created_at.desc()).limit(10000))).scalars().all()
    csv_data = traces_to_csv(rows)
    return Response(content=csv_data, media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=traces.csv"})


@router.get("/{trace_id}/violations", response_model=List[ViolationOut])
async def list_trace_violations(trace_id: str, ctx: AuthContext = Depends(get_current_user),
                                db: AsyncSession = Depends(get_db)):
    raise HTTPException(501, "not_implemented")


@router.get("/{trace_id}", response_model=TraceDetailOut)
async def get_trace(trace_id: str, ctx: AuthContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    t = (await db.execute(
        select(Trace).where(Trace.id == trace_id, Trace.org_id == ctx.org_id)
    )).scalar_one_or_none()
    if not t:
        raise HTTPException(404, "trace_not_found")
    spans = (await db.execute(
        select(Span).where(Span.trace_id == trace_id).order_by(Span.seq, Span.started_at)
    )).scalars().all()
    return TraceDetailOut(**TraceOut.model_validate(t).model_dump(),
                          spans=[SpanOut.model_validate(s) for s in spans])
