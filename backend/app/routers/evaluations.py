from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import AuthContext, get_current_user, require_role
from app.models import (
    Agent,
    EvaluationCase,
    EvaluationResult,
    EvaluationRun,
    EvaluationSet,
)
from app.schemas.evaluation import (
    CaseIn,
    CaseOut,
    EvalDiffOut,
    EvalDiffRequest,
    EvalResultOut,
    EvalRunDetailOut,
    EvalRunOut,
    EvalRunRequest,
    EvalSetCreate,
    EvalSetDetailOut,
    EvalSetOut,
    EvalSetUpdate,
)
from app.services.evaluation_service import run_evaluation

router = APIRouter(prefix="/api/evaluations", tags=["evaluations"])


def _set_out(s: EvaluationSet, case_count: int) -> EvalSetOut:
    out = EvalSetOut.model_validate(s)
    out.case_count = case_count
    return out


@router.get("/sets", response_model=List[EvalSetOut])
async def list_sets(ctx: AuthContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(EvaluationSet).where(EvaluationSet.org_id == ctx.org_id).order_by(EvaluationSet.created_at.desc())
    )).scalars().all()
    out = []
    for s in rows:
        c = (await db.execute(
            select(func.count(EvaluationCase.id)).where(EvaluationCase.set_id == s.id)
        )).scalar_one()
        out.append(_set_out(s, c or 0))
    return out


@router.post("/sets", response_model=EvalSetDetailOut)
async def create_set(
    req: EvalSetCreate,
    ctx: AuthContext = Depends(require_role("developer")),
    db: AsyncSession = Depends(get_db),
):
    s = EvaluationSet(
        org_id=ctx.org_id, name=req.name, description=req.description,
        evaluators=req.evaluators, schedule_cron=req.schedule_cron,
    )
    db.add(s)
    await db.flush()
    cases = [
        EvaluationCase(set_id=s.id, input=c.input, expected_output=c.expected_output,
                       tags=c.tags, weight=c.weight)
        for c in req.cases
    ]
    db.add_all(cases)
    await db.commit()
    await db.refresh(s)
    detail = EvalSetDetailOut.model_validate(s)
    detail.case_count = len(cases)
    detail.cases = [CaseOut.model_validate(c) for c in cases]
    return detail


@router.get("/sets/{set_id}", response_model=EvalSetDetailOut)
async def get_set(set_id: str, ctx: AuthContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    s = (await db.execute(
        select(EvaluationSet).where(EvaluationSet.id == set_id, EvaluationSet.org_id == ctx.org_id)
    )).scalar_one_or_none()
    if not s:
        raise HTTPException(404, "set_not_found")
    cases = (await db.execute(select(EvaluationCase).where(EvaluationCase.set_id == set_id))).scalars().all()
    out = EvalSetDetailOut.model_validate(s)
    out.case_count = len(cases)
    out.cases = [CaseOut.model_validate(c) for c in cases]
    return out


@router.patch("/sets/{set_id}", response_model=EvalSetOut)
async def update_set(set_id: str, req: EvalSetUpdate,
                     ctx: AuthContext = Depends(require_role("developer")),
                     db: AsyncSession = Depends(get_db)):
    s = (await db.execute(select(EvaluationSet).where(EvaluationSet.id == set_id, EvaluationSet.org_id == ctx.org_id))).scalar_one_or_none()
    if not s:
        raise HTTPException(404, "set_not_found")
    for k, v in req.model_dump(exclude_none=True).items():
        setattr(s, k, v)
    await db.commit()
    return _set_out(s, 0)


@router.post("/sets/{set_id}/cases", response_model=CaseOut)
async def add_case(set_id: str, req: CaseIn,
                   ctx: AuthContext = Depends(require_role("developer")),
                   db: AsyncSession = Depends(get_db)):
    s = (await db.execute(select(EvaluationSet).where(EvaluationSet.id == set_id, EvaluationSet.org_id == ctx.org_id))).scalar_one_or_none()
    if not s:
        raise HTTPException(404, "set_not_found")
    c = EvaluationCase(set_id=set_id, input=req.input, expected_output=req.expected_output,
                       tags=req.tags, weight=req.weight)
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return CaseOut.model_validate(c)


@router.post("/sets/{set_id}/run", response_model=EvalRunOut)
async def run_set(
    set_id: str,
    req: EvalRunRequest,
    ctx: AuthContext = Depends(require_role("developer")),
    db: AsyncSession = Depends(get_db),
):
    s = (await db.execute(
        select(EvaluationSet).where(EvaluationSet.id == set_id, EvaluationSet.org_id == ctx.org_id)
    )).scalar_one_or_none()
    if not s:
        raise HTTPException(404, "set_not_found")
    agent = (await db.execute(
        select(Agent).where(Agent.id == req.agent_id, Agent.org_id == ctx.org_id)
    )).scalar_one_or_none()
    if not agent:
        raise HTTPException(404, "agent_not_found")
    run = EvaluationRun(
        set_id=set_id, agent_id=agent.id,
        agent_version=req.agent_version or agent.current_version,
        triggered_by=ctx.user.id,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    # Run inline (still async); for heavier loads Celery worker can pick this up via .delay()
    await run_evaluation(db, run.id)
    await db.refresh(run)
    return EvalRunOut.model_validate(run)


@router.get("/runs", response_model=List[EvalRunOut])
async def list_runs(
    ctx: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    agent_id: str | None = None,
    set_id: str | None = None,
    limit: int = 50,
):
    q = select(EvaluationRun).join(EvaluationSet, EvaluationSet.id == EvaluationRun.set_id).where(
        EvaluationSet.org_id == ctx.org_id
    )
    if agent_id:
        q = q.where(EvaluationRun.agent_id == agent_id)
    if set_id:
        q = q.where(EvaluationRun.set_id == set_id)
    rows = (await db.execute(q.order_by(EvaluationRun.created_at.desc()).limit(limit))).scalars().all()
    return [EvalRunOut.model_validate(r) for r in rows]


@router.get("/runs/{run_id}", response_model=EvalRunDetailOut)
async def get_run(run_id: str, ctx: AuthContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    run = (await db.execute(select(EvaluationRun).where(EvaluationRun.id == run_id))).scalar_one_or_none()
    if not run:
        raise HTTPException(404, "run_not_found")
    s = (await db.execute(select(EvaluationSet).where(EvaluationSet.id == run.set_id, EvaluationSet.org_id == ctx.org_id))).scalar_one_or_none()
    if not s:
        raise HTTPException(404, "run_not_found")
    results = (await db.execute(select(EvaluationResult).where(EvaluationResult.run_id == run.id))).scalars().all()
    out = EvalRunDetailOut.model_validate(run)
    out.results = [EvalResultOut.model_validate(r) for r in results]
    return out


@router.post("/diff", response_model=EvalDiffOut)
async def diff_runs(req: EvalDiffRequest, ctx: AuthContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    own = select(EvaluationRun).join(EvaluationSet, EvaluationSet.id == EvaluationRun.set_id).where(
        EvaluationSet.org_id == ctx.org_id
    )
    a = (await db.execute(own.where(EvaluationRun.id == req.run_a_id))).scalar_one_or_none()
    b = (await db.execute(own.where(EvaluationRun.id == req.run_b_id))).scalar_one_or_none()
    if not a or not b:
        raise HTTPException(404, "run_not_found")
    res_a = (await db.execute(select(EvaluationResult).where(EvaluationResult.run_id == a.id))).scalars().all()
    res_b = (await db.execute(select(EvaluationResult).where(EvaluationResult.run_id == b.id))).scalars().all()
    map_a = {r.case_id: r for r in res_a}
    map_b = {r.case_id: r for r in res_b}
    regressions = []
    improvements = []
    for cid in set(map_a) | set(map_b):
        ra, rb = map_a.get(cid), map_b.get(cid)
        sa = ra.score if ra else 0.0
        sb = rb.score if rb else 0.0
        if sb < sa - 0.05:
            regressions.append({"case_id": cid, "from": sa, "to": sb})
        elif sb > sa + 0.05:
            improvements.append({"case_id": cid, "from": sa, "to": sb})
    return EvalDiffOut(
        run_a=EvalRunOut.model_validate(a),
        run_b=EvalRunOut.model_validate(b),
        delta_score=round(b.score - a.score, 3),
        regressions=regressions,
        improvements=improvements,
    )
