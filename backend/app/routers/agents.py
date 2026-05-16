from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import AuthContext, get_current_user, require_role
from app.models import Agent, AgentDependency, AgentVersion, EvaluationRun, AuditLog
from app.schemas.agent import (
    AgentCreate,
    AgentGraph,
    AgentOut,
    AgentUpdate,
    AgentVersionCreate,
    AgentVersionOut,
    DependencyEdge,
    PromoteRequest,
)
from app.services.agent_service import compute_agent_health, ensure_default_version

router = APIRouter(prefix="/api/agents", tags=["agents"])


async def _agent_out(db: AsyncSession, agent: Agent) -> AgentOut:
    health = await compute_agent_health(db, agent.id)
    out = AgentOut.model_validate(agent)
    out.health = health["health"]
    out.error_rate = health["error_rate"]
    out.p95_latency_ms = health["p95_latency_ms"]
    out.cost_24h = health["cost_24h"]
    return out


@router.get("", response_model=List[AgentOut])
async def list_agents(ctx: AuthContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(Agent).where(Agent.org_id == ctx.org_id).order_by(Agent.created_at.desc())
    )).scalars().all()
    return [await _agent_out(db, a) for a in rows]


@router.post("", response_model=AgentOut)
async def create_agent(
    req: AgentCreate,
    ctx: AuthContext = Depends(require_role("developer")),
    db: AsyncSession = Depends(get_db),
):
    a = Agent(
        org_id=ctx.org_id,
        name=req.name,
        description=req.description,
        model_provider=req.model_provider,
        model_name=req.model_name,
        environment=req.environment,
        tags=req.tags,
        allowed_tools=req.allowed_tools,
        eval_threshold=req.eval_threshold,
    )
    db.add(a)
    await db.commit()
    await db.refresh(a)
    await ensure_default_version(db, a)
    db.add(AuditLog(
        org_id=ctx.org_id, actor_type="user", actor_id=ctx.user.id,
        action="agent_create", resource_type="agent", resource_id=a.id,
    ))
    await db.commit()
    return await _agent_out(db, a)


@router.get("/graph", response_model=AgentGraph)
async def agent_graph(ctx: AuthContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    agents = (await db.execute(select(Agent).where(Agent.org_id == ctx.org_id))).scalars().all()
    aset = {a.id for a in agents}
    edges = (await db.execute(
        select(AgentDependency).where(
            AgentDependency.parent_id.in_(aset),
            AgentDependency.child_id.in_(aset),
        )
    )).scalars().all() if aset else []
    nodes = [await _agent_out(db, a) for a in agents]
    return AgentGraph(
        nodes=nodes,
        edges=[DependencyEdge(parent_id=e.parent_id, child_id=e.child_id, call_count=e.call_count) for e in edges],
    )


@router.get("/{agent_id}", response_model=AgentOut)
async def get_agent(agent_id: str, ctx: AuthContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    a = (await db.execute(
        select(Agent).where(Agent.id == agent_id, Agent.org_id == ctx.org_id)
    )).scalar_one_or_none()
    if not a:
        raise HTTPException(404, "agent_not_found")
    return await _agent_out(db, a)


@router.patch("/{agent_id}", response_model=AgentOut)
async def update_agent(
    agent_id: str,
    req: AgentUpdate,
    ctx: AuthContext = Depends(require_role("developer")),
    db: AsyncSession = Depends(get_db),
):
    a = (await db.execute(select(Agent).where(Agent.id == agent_id, Agent.org_id == ctx.org_id))).scalar_one_or_none()
    if not a:
        raise HTTPException(404, "agent_not_found")
    for k, v in req.model_dump(exclude_none=True).items():
        setattr(a, k, v)
    a.updated_at = datetime.utcnow()
    await db.commit()
    return await _agent_out(db, a)


@router.delete("/{agent_id}")
async def delete_agent(
    agent_id: str,
    ctx: AuthContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    a = (await db.execute(select(Agent).where(Agent.id == agent_id, Agent.org_id == ctx.org_id))).scalar_one_or_none()
    if not a:
        raise HTTPException(404, "agent_not_found")
    await db.delete(a)
    await db.commit()
    return {"ok": True}


@router.get("/{agent_id}/versions", response_model=List[AgentVersionOut])
async def list_versions(agent_id: str, ctx: AuthContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    a = (await db.execute(select(Agent).where(Agent.id == agent_id, Agent.org_id == ctx.org_id))).scalar_one_or_none()
    if not a:
        raise HTTPException(404, "agent_not_found")
    rows = (await db.execute(
        select(AgentVersion).where(AgentVersion.agent_id == agent_id).order_by(AgentVersion.created_at.desc())
    )).scalars().all()
    return [AgentVersionOut.model_validate(r) for r in rows]


@router.post("/{agent_id}/versions", response_model=AgentVersionOut)
async def create_version(
    agent_id: str,
    req: AgentVersionCreate,
    ctx: AuthContext = Depends(require_role("developer")),
    db: AsyncSession = Depends(get_db),
):
    a = (await db.execute(select(Agent).where(Agent.id == agent_id, Agent.org_id == ctx.org_id))).scalar_one_or_none()
    if not a:
        raise HTTPException(404, "agent_not_found")
    v = AgentVersion(
        agent_id=agent_id,
        version=req.version,
        model_name=req.model_name,
        prompt_template=req.prompt_template,
        config=req.config,
        environment=req.environment,
    )
    db.add(v)
    a.current_version = req.version
    a.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(v)
    return AgentVersionOut.model_validate(v)


@router.post("/{agent_id}/promote", response_model=AgentVersionOut)
async def promote(
    agent_id: str,
    req: PromoteRequest,
    ctx: AuthContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    a = (await db.execute(select(Agent).where(Agent.id == agent_id, Agent.org_id == ctx.org_id))).scalar_one_or_none()
    if not a:
        raise HTTPException(404, "agent_not_found")
    if req.target_environment not in {"dev", "staging", "prod"}:
        raise HTTPException(400, "invalid_environment")

    # eval gate: require last completed run >= threshold
    if not req.force and req.target_environment == "prod":
        last = (await db.execute(
            select(EvaluationRun).where(
                EvaluationRun.agent_id == agent_id,
                EvaluationRun.status == "completed",
            ).order_by(EvaluationRun.created_at.desc()).limit(1)
        )).scalar_one_or_none()
        if not last or last.score < (a.eval_threshold or 0.8):
            raise HTTPException(
                400,
                f"eval_gate_blocked: last_score={getattr(last, 'score', None)} threshold={a.eval_threshold}",
            )

    src = (await db.execute(
        select(AgentVersion).where(
            AgentVersion.agent_id == agent_id, AgentVersion.version == req.version
        )
    )).scalar_one_or_none()
    if not src:
        raise HTTPException(404, "version_not_found")

    new_v = AgentVersion(
        agent_id=agent_id,
        version=req.version,
        model_name=src.model_name,
        prompt_template=src.prompt_template,
        config=src.config,
        environment=req.target_environment,
        promoted_from=src.environment,
        promoted_by=ctx.user.id,
    )
    db.add(new_v)
    a.environment = req.target_environment
    a.current_version = req.version
    a.updated_at = datetime.utcnow()
    db.add(AuditLog(
        org_id=ctx.org_id, actor_type="user", actor_id=ctx.user.id,
        action="agent_promote", resource_type="agent", resource_id=agent_id,
        details={"to": req.target_environment, "version": req.version, "force": req.force},
    ))
    await db.commit()
    await db.refresh(new_v)
    return AgentVersionOut.model_validate(new_v)
