from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.middleware.auth import AuthContext, get_current_user, require_role
from app.middleware.rate_limit import enforce_rate_limit
from app.models import ApiKey, OrgMembership, Organization, Session, User
from app.schemas.auth import (
    ApiKeyCreate,
    ApiKeyOut,
    InviteRequest,
    LoginRequest,
    MeResponse,
    MemberOut,
    OrgOut,
    RefreshRequest,
    RegisterRequest,
    SessionOut,
    TokenPair,
    UserOut,
)
from app.services import auth_service

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=TokenPair)
async def register(req: RegisterRequest, request: Request, db: AsyncSession = Depends(get_db)):
    ip = request.client.host if request.client else ""
    await enforce_rate_limit(f"register:{ip}", settings.REGISTER_LIMIT_PER_HOUR, 3600)
    try:
        user, org = await auth_service.register_user(
            db, req.email, req.password, req.full_name, req.org_name
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    user, org, access, refresh, _ = await auth_service.login(
        db, req.email, req.password,
        user_agent=request.headers.get("user-agent", ""),
        ip=ip,
    )
    return TokenPair(access_token=access, refresh_token=refresh)


@router.post("/login", response_model=TokenPair)
async def login(req: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    try:
        user, org, access, refresh, _ = await auth_service.login(
            db, req.email, req.password,
            user_agent=request.headers.get("user-agent", ""),
            ip=request.client.host if request.client else "",
        )
    except ValueError:
        raise HTTPException(status_code=401, detail="invalid_credentials")
    return TokenPair(access_token=access, refresh_token=refresh)


@router.post("/refresh", response_model=TokenPair)
async def refresh(req: RefreshRequest, db: AsyncSession = Depends(get_db)):
    try:
        access, new_refresh = await auth_service.refresh_tokens(db, req.refresh_token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    return TokenPair(access_token=access, refresh_token=new_refresh)


@router.post("/logout")
async def logout(req: RefreshRequest, db: AsyncSession = Depends(get_db)):
    await auth_service.logout(db, req.refresh_token)
    return {"ok": True}


@router.get("/me", response_model=MeResponse)
async def me(ctx: AuthContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    memberships = (await db.execute(
        select(OrgMembership).where(OrgMembership.user_id == ctx.user.id)
    )).scalars().all()
    org_ids = [m.org_id for m in memberships]
    orgs = (await db.execute(select(Organization).where(Organization.id.in_(org_ids)))).scalars().all()
    return MeResponse(
        user=UserOut.model_validate(ctx.user),
        organizations=[OrgOut.model_validate(o) for o in orgs],
        role=ctx.role,
        active_org_id=ctx.org_id,
    )


@router.get("/members", response_model=List[MemberOut])
async def list_members(ctx: AuthContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(OrgMembership, User).join(User, User.id == OrgMembership.user_id)
        .where(OrgMembership.org_id == ctx.org_id)
    )).all()
    return [
        MemberOut(user_id=u.id, email=u.email, full_name=u.full_name, role=m.role)
        for (m, u) in rows
    ]


@router.post("/invite", response_model=MemberOut)
async def invite_member(
    req: InviteRequest,
    ctx: AuthContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    user = (await db.execute(select(User).where(User.email == req.email))).scalar_one_or_none()
    if not user:
        user = User(email=req.email, password_hash=auth_service.hash_password("ChangeMe1234!"), full_name="")
        db.add(user)
        await db.flush()
    existing = (await db.execute(
        select(OrgMembership).where(
            OrgMembership.org_id == ctx.org_id, OrgMembership.user_id == user.id
        )
    )).scalar_one_or_none()
    if existing:
        existing.role = req.role
    else:
        db.add(OrgMembership(org_id=ctx.org_id, user_id=user.id, role=req.role))
    await db.commit()
    return MemberOut(user_id=user.id, email=user.email, full_name=user.full_name, role=req.role)


@router.get("/sessions", response_model=List[SessionOut])
async def list_sessions(ctx: AuthContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(Session).where(Session.user_id == ctx.user.id, Session.revoked_at.is_(None))
        .order_by(Session.created_at.desc())
    )).scalars().all()
    return [SessionOut.model_validate(s) for s in rows]


@router.delete("/sessions/{session_id}")
async def revoke_session(session_id: str, ctx: AuthContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    s = (await db.execute(select(Session).where(Session.id == session_id, Session.user_id == ctx.user.id))).scalar_one_or_none()
    if not s:
        raise HTTPException(404, "not_found")
    s.revoked_at = datetime.utcnow()
    await db.commit()
    return {"ok": True}


@router.get("/api-keys", response_model=List[ApiKeyOut])
async def list_api_keys(ctx: AuthContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(ApiKey).where(ApiKey.org_id == ctx.org_id).order_by(ApiKey.created_at.desc())
    )).scalars().all()
    return [ApiKeyOut.model_validate(r) for r in rows]


@router.post("/api-keys", response_model=ApiKeyOut)
async def create_api_key(
    req: ApiKeyCreate,
    ctx: AuthContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    full, prefix, h = auth_service.generate_api_key()
    key = ApiKey(org_id=ctx.org_id, name=req.name, key_prefix=prefix, key_hash=h)
    db.add(key)
    await db.commit()
    await db.refresh(key)
    out = ApiKeyOut.model_validate(key)
    out.key = full
    return out


@router.delete("/api-keys/{key_id}")
async def revoke_api_key(
    key_id: str,
    ctx: AuthContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    k = (await db.execute(select(ApiKey).where(ApiKey.id == key_id, ApiKey.org_id == ctx.org_id))).scalar_one_or_none()
    if not k:
        raise HTTPException(404, "not_found")
    k.revoked_at = datetime.utcnow()
    await db.commit()
    return {"ok": True}
