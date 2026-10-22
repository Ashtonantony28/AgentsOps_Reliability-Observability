import asyncio
import hashlib
import secrets
import json
from datetime import datetime, timedelta
from typing import Any, Optional, Tuple

from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import database
from app.config import settings
from app.database import get_db
from app.models import User, OrgMembership, ApiKey, Organization

bearer_scheme = HTTPBearer(auto_error=False)

ROLE_ORDER = {"viewer": 1, "developer": 2, "admin": 3, "owner": 4}


def make_access_token(sub: str, org_id: str, role: str) -> str:
    exp = datetime.utcnow() + timedelta(minutes=settings.JWT_ACCESS_TTL_MIN)
    payload = {"sub": sub, "org_id": org_id, "role": role, "exp": exp, "type": "access"}
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def make_refresh_token(sub: str, org_id: str) -> Tuple[str, datetime]:
    exp = datetime.utcnow() + timedelta(days=settings.JWT_REFRESH_TTL_DAYS)
    payload = {"sub": sub, "org_id": org_id, "exp": exp, "type": "refresh", "jti": secrets.token_hex(8)}
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


def _validated(model, body: bytes) -> Any:
    data = json.loads(body)
    try:
        return model.model_validate(data)
    except ValidationError:
        return data  # validated again by FastAPI, for its 422


class _OffLoopJson(Request):
    def __init__(self, request: Request, model):
        super().__init__(request.scope, request.receive)
        self._model = model

    async def json(self) -> Any:
        if not hasattr(self, "_json"):
            self._json = await asyncio.to_thread(_validated, self._model, await self.body())
        return self._json


class ApiKeyRoute(APIRoute):
    """Authenticates the API key (`request.state.api_key`: org, key) before the body is read, then parses and
    validates the body off the event loop (AUDIT-13 A13-2)."""

    def get_route_handler(self):
        handler, model = super().get_route_handler(), self.body_field.type_

        async def route(request: Request) -> Response:
            async with database.async_session_maker() as db:
                request.state.api_key = await authenticate_api_key(request, db)
            return await handler(_OffLoopJson(request, model))
        return route
