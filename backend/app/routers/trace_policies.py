import uuid
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import AuthContext, authenticate_api_key, get_current_user, require_role
from app.middleware.rate_limit import enforce_rate_limit
from app.models import Agent, AuditLog, Organization, TracePolicy
from app.policy.ast import Policy
from app.policy.dsl import PolicyParseError, parse
from app.policy.monitor import plan_for
from app.schemas.policy import (
    PolicyCheckIn,
    PolicyCheckOut,
    PolicyError,
    PolicyValidateIn,
    PolicyValidateOut,
    TracePolicyCreate,
    TracePolicyOut,
    TracePolicyUpdate,
)
from app.services import trace_policy_service as tps
from app.services.trace_service import TraceConflict, precheck

router = APIRouter(prefix="/api/trace-policies", tags=["trace-policies"])


def parse_single(source: str) -> Policy:
    policies = parse(source)
    if len(policies) != 1:
        raise PolicyParseError(1, 1, f"expected exactly one policy block, found {len(policies)}")
    return policies[0]


def _parse_or_422(source: str) -> Policy:
    try:
        return parse_single(source)
    except PolicyParseError as e:
        raise HTTPException(422, detail={
            "error": "invalid_policy", "message": e.message,
            "errors": [{"line": e.line, "col": e.col, "message": e.message}],
        })


def _name_taken(name: str) -> HTTPException:
    return HTTPException(409, detail={"error": "policy_name_taken", "message": f"policy name '{name}' already exists"})


async def _check_name_free(db: AsyncSession, org_id: str, name: str, exclude_id: str | None = None) -> None:
    q = select(TracePolicy.id).where(TracePolicy.org_id == org_id, TracePolicy.name == name,
                                     TracePolicy.archived_at.is_(None))
    if exclude_id:
        q = q.where(TracePolicy.id != exclude_id)
    if (await db.execute(q)).first():
        raise _name_taken(name)


async def _check_org_limits(db: AsyncSession, org_id: str, parsed: Policy, exclude_id: str | None = None) -> None:
    """At most MAX_ORG_POLICIES non-archived policies and MAX_ORG_POLICY_NODES units of per-event work in all
    (monitor nodes and glob costs, `Plan.cost`), so the work of a span rebuild or a batch check is bounded per trace."""
    await db.execute(select(Organization.id).where(Organization.id == org_id).with_for_update())
    q = select(TracePolicy.id, TracePolicy.source).where(TracePolicy.org_id == org_id,
                                                         TracePolicy.archived_at.is_(None))
    if exclude_id:
        q = q.where(TracePolicy.id != exclude_id)
    rows = (await db.execute(q)).all()
    if len(rows) >= tps.MAX_ORG_POLICIES:
        raise HTTPException(422, detail={"error": "policy_limit", "max_policies": tps.MAX_ORG_POLICIES,
                                         "message": f"an org has at most {tps.MAX_ORG_POLICIES} policies"})
    nodes = plan_for(parsed).cost
    for pid, source in rows:
        try:
            nodes += plan_for(tps._compile(pid, source).policy).cost
        except (PolicyParseError, ValueError):
            pass
    if nodes > tps.MAX_ORG_POLICY_NODES:
        raise HTTPException(422, detail={
            "error": "policy_limit", "nodes": nodes, "max_nodes": tps.MAX_ORG_POLICY_NODES,
            "message": f"this org's policies would have {nodes} formula nodes and glob costs in all; at most "
                       f"{tps.MAX_ORG_POLICY_NODES} are allowed"})


async def _check_agent(db: AsyncSession, org_id: str, agent_id: str | None) -> None:
    if agent_id is None:
        return
    found = (await db.execute(select(Agent.id).where(Agent.id == agent_id, Agent.org_id == org_id))).first()
    if not found:
        raise HTTPException(404, "agent_not_found")


async def _get_policy(db: AsyncSession, org_id: str, policy_id: str) -> TracePolicy:
    p = (await db.execute(
        select(TracePolicy).where(TracePolicy.id == policy_id, TracePolicy.org_id == org_id,
                                  TracePolicy.archived_at.is_(None)).with_for_update()
    )).scalar_one_or_none()
    if not p:
        raise HTTPException(404, "policy_not_found")
    return p


async def _commit_or_409(db: AsyncSession, name: str) -> None:
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise _name_taken(name)


def _audit(ctx: AuthContext, action: str, policy_id: str, details: dict) -> AuditLog:
    return AuditLog(
        org_id=ctx.org_id, actor_type="user", actor_id=ctx.user.id,
        action=action, resource_type="trace_policy", resource_id=policy_id, details=details,
    )


@router.get("", response_model=List[TracePolicyOut])
async def list_trace_policies(include_archived: bool = False, ctx: AuthContext = Depends(get_current_user),
                              db: AsyncSession = Depends(get_db)):
    q = select(TracePolicy).where(TracePolicy.org_id == ctx.org_id)
    if not include_archived:
        q = q.where(TracePolicy.archived_at.is_(None))
    q = q.order_by(TracePolicy.name, TracePolicy.archived_at.is_not(None), TracePolicy.archived_at)
    rows = (await db.execute(q)).scalars().all()
    return [TracePolicyOut.model_validate(r) for r in rows]


@router.post("", response_model=TracePolicyOut)
async def create_trace_policy(
    req: TracePolicyCreate,
    ctx: AuthContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    parsed = _parse_or_422(req.source)
    await _check_agent(db, ctx.org_id, req.agent_id)
    await _check_name_free(db, ctx.org_id, parsed.name)
    await _check_org_limits(db, ctx.org_id, parsed)
    p = TracePolicy(
        org_id=ctx.org_id, name=parsed.name, source=req.source, severity=parsed.severity,
        action=req.action, agent_id=req.agent_id, environment=req.environment, enabled=req.enabled, version=1,
    )
    p.id = str(uuid.uuid4())
    db.add(p)
    db.add(_audit(ctx, "trace_policy_create", p.id, {"name": p.name, "action": p.action}))
    await _commit_or_409(db, parsed.name)
    await db.refresh(p)
    return TracePolicyOut.model_validate(p)


@router.post("/validate", response_model=PolicyValidateOut)
async def validate_trace_policy(
    req: PolicyValidateIn,
    ctx: AuthContext = Depends(get_current_user),
):
    try:
        parse_single(req.source)
    except PolicyParseError as e:
        return PolicyValidateOut(ok=False, errors=[PolicyError(line=e.line, col=e.col, message=e.message)])
    return PolicyValidateOut(ok=True, errors=[])


@router.post("/check", response_model=PolicyCheckOut)
async def check_candidate(req: PolicyCheckIn, request: Request, db: AsyncSession = Depends(get_db)):
    org, _api_key = await authenticate_api_key(request, db)
    await enforce_rate_limit(f"check:{org.id}", org.rate_limit_per_min)
    try:
        return await precheck(db, org.id, req)
    except TraceConflict as e:
        raise HTTPException(e.status, detail={"error": e.code, "message": e.message, **e.details})
    except ValueError as e:
        raise HTTPException(404, detail=str(e))


@router.put("/{policy_id}", response_model=TracePolicyOut)
async def update_trace_policy(
    policy_id: str,
    req: TracePolicyUpdate,
    ctx: AuthContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    p = await _get_policy(db, ctx.org_id, policy_id)
    fields = req.model_dump(exclude_unset=True)
    changes: dict = {}
    source = fields.pop("source", None)
    if source is not None and source != p.source:
        parsed = _parse_or_422(source)
        if parsed.name != p.name:
            await _check_name_free(db, ctx.org_id, parsed.name, exclude_id=p.id)
        await _check_org_limits(db, ctx.org_id, parsed, exclude_id=p.id)
        changes.update(source=source, name=parsed.name, severity=parsed.severity, version=p.version + 1)
    if "agent_id" in fields:
        await _check_agent(db, ctx.org_id, fields["agent_id"])
    for key in ("action", "enabled"):
        if fields.get(key) is not None:
            changes[key] = fields[key]
    for key in ("agent_id", "environment"):
        if key in fields:
            changes[key] = fields[key]
    changes = {k: v for k, v in changes.items() if getattr(p, k) != v}
    for k, v in changes.items():
        setattr(p, k, v)
    p.updated_at = datetime.utcnow()
    db.add(_audit(ctx, "trace_policy_update", p.id, changes))
    await _commit_or_409(db, p.name)
    await db.refresh(p)
    return TracePolicyOut.model_validate(p)


@router.delete("/{policy_id}")
async def delete_trace_policy(
    policy_id: str,
    ctx: AuthContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    p = await _get_policy(db, ctx.org_id, policy_id)
    db.add(_audit(ctx, "trace_policy_delete", p.id, {"name": p.name}))
    p.archived_at = datetime.utcnow()  # archived, not deleted, so its violations stay in alerts and reports
    await db.commit()
    return {"ok": True}
