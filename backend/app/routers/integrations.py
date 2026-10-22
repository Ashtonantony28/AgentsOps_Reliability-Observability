import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta
from typing import List

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.middleware.auth import ROLE_ORDER, AuthContext, get_current_user, hash_token, require_role
from app.models import AuditLog, Integration, OAuthState, OrgMembership, User
from app.schemas.integration import (
    IntegrationCreate,
    IntegrationCreatedOut,
    IntegrationOut,
    IntegrationUpdate,
    SigningSecretOut,
    SlackOAuthCallbackOut,
    SlackOAuthStartOut,
    TestConnectionResult,
)
from app.services import outbound

OAUTH_STATE_TTL = timedelta(minutes=10)

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


@router.get("", response_model=List[IntegrationOut])
async def list_integrations(ctx: AuthContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(Integration).where(Integration.org_id == ctx.org_id).order_by(Integration.created_at.desc())
    )).scalars().all()
    # redact secrets
    out = []
    for r in rows:
        d = IntegrationOut.model_validate(r)
        d.config = {k: ("***" if "key" in k.lower() or "secret" in k.lower() or "token" in k.lower() else v)
                    for k, v in (d.config or {}).items()}
        out.append(d)
    return out


@router.post("", response_model=IntegrationCreatedOut)
async def create_integration(
    req: IntegrationCreate,
    ctx: AuthContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    i = Integration(org_id=ctx.org_id, kind=req.kind, name=req.name, config=req.config,
                    signing_secret=secrets.token_hex(32))
    db.add(i)
    await db.commit()
    await db.refresh(i)
    return IntegrationCreatedOut.model_validate(i)


@router.patch("/{int_id}", response_model=IntegrationOut)
async def update_integration(int_id: str, req: IntegrationUpdate,
                             ctx: AuthContext = Depends(require_role("admin")),
                             db: AsyncSession = Depends(get_db)):
    i = (await db.execute(select(Integration).where(Integration.id == int_id, Integration.org_id == ctx.org_id))).scalar_one_or_none()
    if not i:
        raise HTTPException(404, "integration_not_found")
    for k, v in req.model_dump(exclude_none=True).items():
        setattr(i, k, v)
    await db.commit()
    return IntegrationOut.model_validate(i)


@router.delete("/{int_id}")
async def delete_integration(int_id: str, ctx: AuthContext = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    i = (await db.execute(select(Integration).where(Integration.id == int_id, Integration.org_id == ctx.org_id))).scalar_one_or_none()
    if not i:
        raise HTTPException(404, "integration_not_found")
    await db.delete(i)
    await db.commit()
    return {"ok": True}


@router.post("/{int_id}/signing-secret", response_model=SigningSecretOut)
async def rotate_signing_secret(int_id: str, ctx: AuthContext = Depends(require_role("admin")),
                                db: AsyncSession = Depends(get_db)):
    i = (await db.execute(select(Integration).where(Integration.id == int_id, Integration.org_id == ctx.org_id))).scalar_one_or_none()
    if not i:
        raise HTTPException(404, "integration_not_found")
    i.signing_secret = secrets.token_hex(32)
    await db.commit()
    return SigningSecretOut(signing_secret=i.signing_secret)


@router.post("/{int_id}/test", response_model=TestConnectionResult)
async def test_connection(int_id: str, ctx: AuthContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    i = (await db.execute(select(Integration).where(Integration.id == int_id, Integration.org_id == ctx.org_id))).scalar_one_or_none()
    if not i:
        raise HTTPException(404, "integration_not_found")
    outbound.org.set(ctx.org_id)
    cfg = i.config or {}
    ok, message, details = False, "unknown", {}
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            if i.kind == "slack":
                url = cfg.get("webhook_url")
                if not url:
                    raise ValueError("missing webhook_url")
                r = await outbound.post(url, json={"text": "AgentOps connection test"})
                ok = r.status_code < 400
                message = "ok" if ok else f"status={r.status_code}"
            elif i.kind in ("openai", "anthropic", "cohere"):
                # Don't actually call paid APIs in test; verify presence of key
                k = cfg.get("api_key", "")
                ok = bool(k)
                message = "key_present" if ok else "missing_api_key"
            elif i.kind == "webhook":
                url = cfg.get("url")
                if not url:
                    raise ValueError("missing url")
                r = await outbound.post(url, json={"event": "test"})
                ok = r.status_code < 400
                message = f"status={r.status_code}"
            elif i.kind == "pagerduty":
                ok = bool(cfg.get("routing_key"))
                message = "routing_key_present" if ok else "missing routing_key"
            elif i.kind == "datadog":
                api_key = cfg.get("api_key")
                if not api_key:
                    raise ValueError("missing api_key")
                r = await c.get("https://api.datadoghq.com/api/v1/validate",
                                headers={"DD-API-KEY": api_key})
                ok = r.status_code == 200
                message = "ok" if ok else f"status={r.status_code}"
            else:
                ok = True
                message = "no specific check; configured"
    except Exception as e:
        ok = False
        message = str(e)

    i.last_sync_at = datetime.utcnow()
    i.last_status = "ok" if ok else "error"
    if not ok:
        i.error_count += 1
        i.last_error = message[:500]
    else:
        i.last_error = ""
    await db.commit()
    return TestConnectionResult(ok=ok, message=message, details=details)


@router.post("/webhooks/{kind}/{org_id}", openapi_extra={"requestBody": {
    "required": True, "content": {"application/json": {"schema": {"type": "object"}}}}})
async def receive_webhook(kind: str, org_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Generic webhook receiver for external systems, signed with an integration's secret. Logs the event in the audit
    log."""
    body = await request.body()
    signature = request.headers.get("x-agentops-signature", "").encode()
    rows = (await db.execute(select(Integration.id, Integration.signing_secret).where(
        Integration.org_id == org_id, Integration.kind == kind, Integration.enabled.is_(True),
        Integration.signing_secret.is_not(None),
    ))).all()
    matched = None
    for int_id, secret in rows:
        expected = b"sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest().encode()
        if hmac.compare_digest(expected, signature):
            matched = int_id
    if matched is None:  # also an unknown org or kind, so the answer does not say which exist
        raise HTTPException(401, "invalid_signature")
    try:
        payload = json.loads(body)
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        raise HTTPException(422, "invalid_payload")
    db.add(AuditLog(
        org_id=org_id, actor_type="system", actor_id=kind,
        action="webhook_received", resource_type="integration", resource_id=matched,
        details=payload,
    ))
    await db.commit()
    return {"ok": True}


@router.get("/oauth/slack/start", response_model=SlackOAuthStartOut)
async def slack_oauth_start(ctx: AuthContext = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    if not settings.SLACK_CLIENT_ID:
        raise HTTPException(400, "slack_oauth_not_configured")
    state, now = secrets.token_urlsafe(32), datetime.utcnow()
    await db.execute(delete(OAuthState).where(OAuthState.expires_at <= now))
    db.add(OAuthState(provider="slack", state_hash=hash_token(state), org_id=ctx.org_id, user_id=ctx.user.id,
                      expires_at=now + OAUTH_STATE_TTL))
    await db.commit()
    url = (
        f"https://slack.com/oauth/v2/authorize?client_id={settings.SLACK_CLIENT_ID}"
        f"&scope=incoming-webhook,chat:write&state={state}"
    )
    return {"url": url}


async def _slack_exchange(code: str) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.post("https://slack.com/api/oauth.v2.access", data={
            "code": code,
            "client_id": settings.SLACK_CLIENT_ID,
            "client_secret": settings.SLACK_CLIENT_SECRET,
        })
    return r.json()


@router.get("/oauth/slack/callback", response_model=SlackOAuthCallbackOut)
async def slack_oauth_callback(code: str, state: str, db: AsyncSession = Depends(get_db)):
    """Complete Slack OAuth exchange and persist webhook URL in the org of the admin who started it."""
    if not (settings.SLACK_CLIENT_ID and settings.SLACK_CLIENT_SECRET):
        raise HTTPException(400, "slack_oauth_not_configured")
    h, now = hash_token(state), datetime.utcnow()
    used = await db.execute(update(OAuthState).where(
        OAuthState.state_hash == h, OAuthState.provider == "slack", OAuthState.used_at.is_(None),
        OAuthState.expires_at > now,
    ).values(used_at=now))
    await db.commit()
    if used.rowcount != 1:
        raise HTTPException(400, "invalid_state")
    st = (await db.execute(select(OAuthState).where(OAuthState.state_hash == h))).scalar_one()
    role = (await db.execute(
        select(OrgMembership.role).join(User, User.id == OrgMembership.user_id)
        .where(OrgMembership.org_id == st.org_id, OrgMembership.user_id == st.user_id, User.is_active.is_(True))
    )).scalar_one_or_none()
    if ROLE_ORDER.get(role, 0) < ROLE_ORDER["admin"]:
        raise HTTPException(400, "invalid_state")
    data = await _slack_exchange(code)
    if not data.get("ok"):
        raise HTTPException(400, f"slack_error: {data.get('error')}")
    webhook = (data.get("incoming_webhook") or {}).get("url")
    integ = Integration(
        org_id=st.org_id, kind="slack", name="Slack",
        config={"webhook_url": webhook, "team": (data.get("team") or {}).get("name", "")},
        signing_secret=secrets.token_hex(32),
    )
    db.add(integ)
    await db.commit()
    return {"ok": True, "team": (data.get("team") or {}).get("name", "")}
