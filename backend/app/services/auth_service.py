import asyncio
import re
import secrets
from datetime import datetime, timedelta
from typing import Tuple, Optional

from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.middleware.auth import (
    hash_api_key,
    hash_token,
    make_access_token,
    make_refresh_token,
)
from app.models import ApiKey, OrgMembership, Organization, Session, User

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


def _slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
    return s or f"org-{secrets.token_hex(3)}"


def hash_password(pw: str) -> str:
    return pwd_ctx.hash(pw)


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return pwd_ctx.verify(pw, hashed)
    except Exception:
        return False


async def register_user(
    db: AsyncSession,
    email: str,
    password: str,
    full_name: str = "",
    org_name: Optional[str] = None,
) -> Tuple[User, Organization]:
    existing = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if existing:
        raise ValueError("email_exists")

    password_hash = await asyncio.to_thread(hash_password, password)  # bcrypt, 0.3 s
    user = User(email=email, password_hash=password_hash, full_name=full_name or email.split("@")[0])
    db.add(user)
    await db.flush()

    slug_base = _slug(org_name or f"{user.full_name}'s org")
    slug = slug_base
    n = 0
    while (await db.execute(select(Organization).where(Organization.slug == slug))).scalar_one_or_none():
        n += 1
        slug = f"{slug_base}-{n}"

    org = Organization(name=org_name or f"{user.full_name}'s org", slug=slug)
    db.add(org)
    await db.flush()

    db.add(OrgMembership(org_id=org.id, user_id=user.id, role="owner"))
    await db.commit()
    return user, org


async def login(
    db: AsyncSession, email: str, password: str, user_agent: str = "", ip: str = ""
) -> Tuple[User, Organization, str, str, str]:
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if not user or not await asyncio.to_thread(verify_password, password, user.password_hash) or not user.is_active:
        raise ValueError("invalid_credentials")

    membership = (await db.execute(
        select(OrgMembership).where(OrgMembership.user_id == user.id).limit(1)
    )).scalar_one_or_none()
    if not membership:
        # Auto-create org for legacy users
        org = Organization(name=f"{user.full_name}'s org", slug=_slug(user.full_name + secrets.token_hex(2)))
        db.add(org)
        await db.flush()
        membership = OrgMembership(org_id=org.id, user_id=user.id, role="owner")
        db.add(membership)
        await db.flush()

    org = (await db.execute(select(Organization).where(Organization.id == membership.org_id))).scalar_one()
    access = make_access_token(user.id, org.id, membership.role)
    refresh, exp = make_refresh_token(user.id)

    db.add(Session(
        user_id=user.id,
        refresh_token_hash=hash_token(refresh),
        user_agent=user_agent[:512],
        ip_address=ip[:64],
        expires_at=exp,
    ))
    user.last_login_at = datetime.utcnow()
    await db.commit()
    return user, org, access, refresh, membership.role


async def refresh_tokens(db: AsyncSession, refresh_token: str) -> Tuple[str, str]:
    from app.middleware.auth import decode_token
    payload = decode_token(refresh_token)
    if payload.get("type") != "refresh":
        raise ValueError("invalid_token_type")

    h = hash_token(refresh_token)
    session = (await db.execute(select(Session).where(Session.refresh_token_hash == h))).scalar_one_or_none()
    if not session or session.revoked_at or session.expires_at < datetime.utcnow():
        raise ValueError("invalid_session")

    user_id = payload["sub"]
    membership = (await db.execute(
        select(OrgMembership).where(OrgMembership.user_id == user_id).limit(1)
    )).scalar_one_or_none()
    if not membership:
        raise ValueError("no_org")

    access = make_access_token(user_id, membership.org_id, membership.role)
    new_refresh, exp = make_refresh_token(user_id)
    session.revoked_at = datetime.utcnow()
    db.add(Session(
        user_id=user_id,
        refresh_token_hash=hash_token(new_refresh),
        user_agent=session.user_agent,
        ip_address=session.ip_address,
        expires_at=exp,
    ))
    await db.commit()
    return access, new_refresh


async def logout(db: AsyncSession, refresh_token: str) -> None:
    h = hash_token(refresh_token)
    session = (await db.execute(select(Session).where(Session.refresh_token_hash == h))).scalar_one_or_none()
    if session and not session.revoked_at:
        session.revoked_at = datetime.utcnow()
        await db.commit()


def generate_api_key() -> Tuple[str, str, str]:
    """Returns (full_key, prefix, hash)."""
    raw = "aop_" + secrets.token_urlsafe(32)
    return raw, raw[:8], hash_api_key(raw)
