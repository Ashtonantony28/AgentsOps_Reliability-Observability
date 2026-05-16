import hashlib
from datetime import datetime, timedelta
from typing import Optional, Tuple

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import User, OrgMembership, ApiKey, Organization

bearer_scheme = HTTPBearer(auto_error=False)

ROLE_ORDER = {"viewer": 1, "developer": 2, "admin": 3, "owner": 4}


def make_access_token(sub: str, org_id: str, role: str) -> str:
    exp = datetime.utcnow() + timedelta(minutes=settings.JWT_ACCESS_TTL_MIN)
    payload = {"sub": sub, "org_id": org_id, "role": role, "exp": exp, "type": "access"}
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def make_refresh_token(sub: str) -> Tuple[str, datetime]:
    exp = datetime.utcnow() + timedelta(days=settings.JWT_REFRESH_TTL_DAYS)
    payload = {"sub": sub, "exp": exp, "type": "refresh"}
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM), exp


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except JWTError as e:
        raise HTTPException(status_code=401, detail="invalid_token") from e


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class AuthContext:
    def __init__(self, user: User, org_id: str, role: str):
        self.user = user
        self.org_id = org_id
        self.role = role

    def require_role(self, min_role: str):
        if ROLE_ORDER.get(self.role, 0) < ROLE_ORDER.get(min_role, 0):
            raise HTTPException(status_code=403, detail="insufficient_role")


async def get_current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> AuthContext:
    if not creds:
        raise HTTPException(status_code=401, detail="missing_auth")
    payload = decode_token(creds.credentials)
    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="invalid_token_type")

    user_id = payload.get("sub")
    org_id = payload.get("org_id")
    role = payload.get("role", "viewer")

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="user_inactive")

    membership = (await db.execute(
        select(OrgMembership).where(
            OrgMembership.user_id == user_id,
            OrgMembership.org_id == org_id,
        )
    )).scalar_one_or_none()
    if not membership:
        raise HTTPException(status_code=403, detail="org_membership_required")

    return AuthContext(user=user, org_id=org_id, role=membership.role or role)


def require_role(min_role: str):
    async def _dep(ctx: AuthContext = Depends(get_current_user)) -> AuthContext:
        ctx.require_role(min_role)
        return ctx
    return _dep


async def authenticate_api_key(
    request: Request,
    db: AsyncSession,
) -> Tuple[Organization, ApiKey]:
    header = request.headers.get("x-api-key") or request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        header = header[7:]
    if not header:
        raise HTTPException(status_code=401, detail="missing_api_key")
    key_h = hash_api_key(header)
    api_key = (await db.execute(select(ApiKey).where(ApiKey.key_hash == key_h))).scalar_one_or_none()
    if not api_key or api_key.revoked_at is not None:
        raise HTTPException(status_code=401, detail="invalid_api_key")
    org = (await db.execute(select(Organization).where(Organization.id == api_key.org_id))).scalar_one_or_none()
    if not org:
        raise HTTPException(status_code=401, detail="org_not_found")
    api_key.last_used_at = datetime.utcnow()
    await db.commit()
    return org, api_key
