from datetime import datetime
from typing import List

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.middleware.auth import AuthContext, get_current_user, require_role
from app.models import Integration
from app.schemas.integration import (
    IntegrationCreate,
    IntegrationOut,
    IntegrationUpdate,
    TestConnectionResult,
)

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


@router.post("", response_model=IntegrationOut)
async def create_integration(
    req: IntegrationCreate,
    ctx: AuthContext = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    i = Integration(org_id=ctx.org_id, kind=req.kind, name=req.name, config=req.config)
    db.add(i)
    await db.commit()
    await db.refresh(i)
    return IntegrationOut.model_validate(i)


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


@router.post("/{int_id}/test", response_model=TestConnectionResult)
async def test_connection(int_id: str, ctx: AuthContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    i = (await db.execute(select(Integration).where(Integration.id == int_id, Integration.org_id == ctx.org_id))).scalar_one_or_none()
    if not i:
        raise HTTPException(404, "integration_not_found")
    cfg = i.config or {}
    ok, message, details = False, "unknown", {}
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            if i.kind == "slack":
                url = cfg.get("webhook_url")
                if not url:
                    raise ValueError("missing webhook_url")
                r = await c.post(url, json={"text": "AgentOps connection test"})
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
                r = await c.post(url, json={"event": "test"})
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


@router.post("/webhooks/{kind}/{org_id}")
async def receive_webhook(kind: str, org_id: str, payload: dict, db: AsyncSession = Depends(get_db)):
    """Generic webhook receiver for external systems. Logs event in audit log."""
    from app.models import AuditLog
    db.add(AuditLog(
        org_id=org_id, actor_type="system", actor_id=kind,
        action="webhook_received", resource_type="integration",
        details=payload,
    ))
    await db.commit()
    return {"ok": True}


@router.get("/oauth/slack/start")
async def slack_oauth_start(ctx: AuthContext = Depends(require_role("admin"))):
    if not settings.SLACK_CLIENT_ID:
        raise HTTPException(400, "slack_oauth_not_configured")
    url = (
        f"https://slack.com/oauth/v2/authorize?client_id={settings.SLACK_CLIENT_ID}"
        f"&scope=incoming-webhook,chat:write&state={ctx.org_id}"
    )
    return {"url": url}


@router.get("/oauth/slack/callback")
async def slack_oauth_callback(code: str, state: str, db: AsyncSession = Depends(get_db)):
    """Complete Slack OAuth exchange and persist webhook URL."""
    if not (settings.SLACK_CLIENT_ID and settings.SLACK_CLIENT_SECRET):
        raise HTTPException(400, "slack_oauth_not_configured")
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.post("https://slack.com/api/oauth.v2.access", data={
            "code": code,
            "client_id": settings.SLACK_CLIENT_ID,
            "client_secret": settings.SLACK_CLIENT_SECRET,
        })
    data = r.json()
    if not data.get("ok"):
        raise HTTPException(400, f"slack_error: {data.get('error')}")
    webhook = (data.get("incoming_webhook") or {}).get("url")
    integ = Integration(
        org_id=state, kind="slack", name="Slack",
        config={"webhook_url": webhook, "team": (data.get("team") or {}).get("name", "")},
    )
    db.add(integ)
    await db.commit()
    return {"ok": True, "team": (data.get("team") or {}).get("name", "")}
