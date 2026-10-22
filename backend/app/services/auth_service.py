import asyncio
import re
import secrets
from datetime import datetime, timedelta
from typing import Tuple, Optional

from passlib.context import CryptContext
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.middleware.auth import (
    hash_api_key,
    hash_token,
    make_access_token,
    make_refresh_token,
)
from app.models import ApiKey, Invite, OrgMembership, Organization, Session, User
from app.schemas.auth import InviteOut

INVITE_TTL_HOURS = 72
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
    access, refresh = _issue(db, user.id, org.id, membership.role, user_agent, ip)
    user.last_login_at = datetime.utcnow()
    await db.commit()
    return user, org, access, refresh, membership.role


def _issue(db: AsyncSession, user_id: str, org_id: str, role: str, user_agent: str = "", ip: str = "") -> Tuple[str, str]:
    """A token pair for `org_id` and its session row (added, not committed)."""
    refresh, exp = make_refresh_token(user_id, org_id)
    db.add(Session(
        user_id=user_id,
        refresh_token_hash=hash_token(refresh),
        user_agent=user_agent[:512],
        ip_address=ip[:64],
        expires_at=exp,
    ))
    return make_access_token(user_id, org_id, role), refresh


async def refresh_tokens(db: AsyncSession, refresh_token: str) -> Tuple[str, str]:
    from app.middleware.auth import decode_token
    payload = decode_token(refresh_token)
    if payload.get("type") != "refresh":
        raise ValueError("invalid_token_type")

    h = hash_token(refresh_token)
    session = (await db.execute(select(Session).where(Session.refresh_token_hash == h))).scalar_one_or_none()
    if not session or session.revoked_at or session.expires_at < datetime.utcnow():
        raise ValueError("invalid_session")

    user_id, org_id = payload["sub"], payload.get("org_id")
    if not org_id:  # issued before refresh tokens carried their org
        raise ValueError("invalid_session")
    membership = (await db.execute(
        select(OrgMembership).where(OrgMembership.user_id == user_id, OrgMembership.org_id == org_id)
    )).scalar_one_or_none()
    if not membership:
        raise ValueError("no_org")

    session.revoked_at = datetime.utcnow()
    access, new_refresh = _issue(db, user_id, org_id, membership.role, session.user_agent, session.ip_address)
    await db.commit()
    return access, new_refresh


async def invite(db: AsyncSession, org_id: str, inviter: User, inviter_role: str, email: str,
                 role: str) -> Tuple[InviteOut, Optional[str]]:
    """Adds an existing account to the org, or stores a pending invite and returns its token (for the email only)."""
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user:
        existing = (await db.execute(
            select(OrgMembership).where(OrgMembership.org_id == org_id, OrgMembership.user_id == user.id)
        )).scalar_one_or_none()
        if existing and existing.role == "owner" and inviter_role != "owner":
            raise PermissionError("insufficient_role")
        if existing:
            existing.role = role
        else:
            db.add(OrgMembership(org_id=org_id, user_id=user.id, role=role))
        await db.commit()
        return InviteOut(email=email, role=role, status="added", invite_id=None, expires_at=None), None

    await db.execute(delete(Invite).where(Invite.org_id == org_id, Invite.email == email, Invite.accepted_at.is_(None)))
    token = secrets.token_urlsafe(32)
    inv = Invite(org_id=org_id, email=email, role=role, token_hash=hash_token(token), invited_by=inviter.id,
                 expires_at=datetime.utcnow() + timedelta(hours=INVITE_TTL_HOURS))
    db.add(inv)
    await db.commit()
    return InviteOut(email=email, role=role, status="pending", invite_id=inv.id, expires_at=inv.expires_at), token


async def accept_invite(db: AsyncSession, token: str, password: str, user_agent: str = "",
                        ip: str = "") -> Tuple[str, str]:
    h, now = hash_token(token), datetime.utcnow()
    inv = (await db.execute(select(Invite).where(
        Invite.token_hash == h, Invite.accepted_at.is_(None), Invite.expires_at > now,
    ))).scalar_one_or_none()
    if not inv:
        raise ValueError("invalid_invite")
    if (await db.execute(select(User.id).where(User.email == inv.email))).first():
        raise LookupError("email_exists")
    password_hash = await asyncio.to_thread(hash_password, password)
    # single use: of two concurrent accepts, one updates the row
    used = await db.execute(update(Invite).where(Invite.id == inv.id, Invite.accepted_at.is_(None))
                            .values(accepted_at=datetime.utcnow()))
    if used.rowcount != 1:
        await db.rollback()
        raise ValueError("invalid_invite")
    user = User(email=inv.email, password_hash=password_hash, full_name=inv.email.split("@")[0],
                last_login_at=datetime.utcnow())
    db.add(user)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise LookupError("email_exists")
    db.add(OrgMembership(org_id=inv.org_id, user_id=user.id, role=inv.role))
    pair = _issue(db, user.id, inv.org_id, inv.role, user_agent, ip)
    await db.commit()
    return pair


async def change_password(db: AsyncSession, user: User, org_id: str, role: str, current: str, new: str,
                          user_agent: str = "", ip: str = "") -> Tuple[str, str]:
    if not await asyncio.to_thread(verify_password, current, user.password_hash):
        raise ValueError("wrong_password")
    user.password_hash = await asyncio.to_thread(hash_password, new)
    await db.execute(update(Session).where(Session.user_id == user.id, Session.revoked_at.is_(None))
                     .values(revoked_at=datetime.utcnow()))
    pair = _issue(db, user.id, org_id, role, user_agent, ip)
    await db.commit()
    return pair


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
