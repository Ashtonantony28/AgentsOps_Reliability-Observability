import asyncio
import json
import logging
from typing import List

import httpx

from app.config import settings
from app.services import outbound

log = logging.getLogger(__name__)


async def send_slack(webhook_url: str, text: str) -> bool:
    try:
        r = await outbound.post(webhook_url, json={"text": text})
        return r.status_code < 400
    except Exception as e:
        log.warning("slack send failed: %s", e)
        return False


async def send_pagerduty(routing_key: str, summary: str, severity: str = "warning") -> bool:
    payload = {
        "routing_key": routing_key,
        "event_action": "trigger",
        "payload": {
            "summary": summary,
            "severity": severity,
            "source": "agentops",
        },
    }
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.post("https://events.pagerduty.com/v2/enqueue", json=payload)
            return r.status_code < 400
    except Exception as e:
        log.warning("pagerduty send failed: %s", e)
        return False


async def send_webhook(url: str, body: dict) -> bool:
    try:
        r = await outbound.post(url, json=body)
        return r.status_code < 400
    except Exception as e:
        log.warning("webhook send failed: %s", e)
        return False


async def send_email(to_addr: str, subject: str, body: str) -> bool:
    if not settings.SMTP_HOST:
        log.info("[email-dryrun] to=%s subject=%s", to_addr, subject)
        return True
    try:
        import aiosmtplib
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["From"] = settings.SMTP_USER or "noreply@agentops.dev"
        msg["To"] = to_addr
        msg["Subject"] = subject
        msg.set_content(body)
        await aiosmtplib.send(
            msg,
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USER or None,
            password=settings.SMTP_PASSWORD or None,
            start_tls=True,
        )
        return True
    except Exception as e:
        log.warning("email send failed: %s", e)
        return False


async def dispatch_alert(channels: List[dict], message: str, severity: str) -> List[bool]:
    results = []
    for ch in channels or []:
        kind = ch.get("type")
        target = ch.get("target") or ch.get("url") or ch.get("email") or ""
        ok = False
        if kind == "slack":
            ok = await send_slack(target, message)
        elif kind == "pagerduty":
            ok = await send_pagerduty(target, message, severity)
        elif kind == "webhook":
            ok = await send_webhook(target, {"message": message, "severity": severity})
        elif kind == "email":
            ok = await send_email(target, f"[AgentOps {severity}] alert", message)
        results.append(ok)
    return results
