import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, Query
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session_maker
from app.middleware.auth import decode_token
from app.models import OrgMembership, ApiKey, Organization
from app.middleware.auth import hash_api_key
from app.middleware.rate_limit import enforce_rate_limit
from app.redis import get_redis
from app.services.trace_service import PolicyViolation, TraceConflict, append_events, ingest_trace
from app.schemas.policy import ViolationOut, WsEventsMessage
from app.schemas.trace import TraceIn

log = logging.getLogger(__name__)
router = APIRouter()


async def _authenticate_ws(token: str | None, api_key: str | None) -> tuple[str | None, str | None]:
    """Returns (org_id, mode) where mode in {"jwt","api_key"}."""
    if token:
        try:
            payload = decode_token(token)
            return payload.get("org_id"), "jwt"
        except Exception:
            return None, None
    if api_key:
        h = hash_api_key(api_key)
        async with async_session_maker() as db:
            ak = (await db.execute(select(ApiKey).where(ApiKey.key_hash == h))).scalar_one_or_none()
            if ak and not ak.revoked_at:
                return ak.org_id, "api_key"
    return None, None


@router.websocket("/ws/traces")
async def ws_traces_feed(
    websocket: WebSocket,
    token: str | None = Query(default=None),
):
    """Live trace feed (JWT-authed). Streams the org's trace and policy-violation pubsub messages."""
    org_id, _mode = await _authenticate_ws(token, None)
    if not org_id:
        await websocket.close(code=4401)
        return
    pubsub = get_redis().pubsub()
    channels = {f"traces:{org_id}": "trace", f"violations:{org_id}": "violation"}
    try:
        await pubsub.subscribe(*channels)  # before accept, so nothing published after the handshake is missed
        await websocket.accept()
        # heartbeat task
        async def heartbeat():
            while True:
                await asyncio.sleep(20)
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    break

        hb = asyncio.create_task(heartbeat())
        try:
            async for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                channel, data = msg.get("channel"), msg.get("data")
                if isinstance(channel, bytes):
                    channel = channel.decode()
                if isinstance(data, bytes):
                    data = data.decode()
                await websocket.send_text(json.dumps({"type": channels[channel], "data": json.loads(data)}))
        finally:
            hb.cancel()
    except WebSocketDisconnect:
        pass
    finally:
        try:
            await pubsub.unsubscribe(*channels)
            await pubsub.close()
        except Exception:
            pass


def _is_events_message(raw: str) -> bool:
    try:
        data = json.loads(raw)
    except ValueError:
        return False
    return isinstance(data, dict) and data.get("type") == "events"


async def _ingest_message(db, org_id: str, raw: str) -> dict:
    if _is_events_message(raw):
        try:
            msg = WsEventsMessage.model_validate_json(raw)
        except Exception as e:
            return {"ok": False, "error": f"invalid_payload: {e}"}
        try:
            trace, rows = await append_events(db, org_id, msg.trace_uid, msg)
        except PolicyViolation as pv:
            return {"ok": False, "error": "policy_block", "rule": pv.rule_name, "target": pv.target}
        except TraceConflict as tc:
            return {"ok": False, "error": tc.code, "message": tc.message, **tc.details}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "id": trace.id, "accepted": len(msg.events),
                "violations": [ViolationOut.model_validate(r).model_dump(mode="json") for r in rows]}
    try:
        payload = TraceIn.model_validate_json(raw)
    except Exception as e:
        return {"ok": False, "error": f"invalid_payload: {e}"}
    try:
        trace = await ingest_trace(db, org_id, payload)
    except PolicyViolation as pv:
        return {"ok": False, "error": "policy_block", "rule": pv.rule_name}
    except TraceConflict as tc:
        return {"ok": False, "error": tc.code, "message": tc.message, **tc.details}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "id": trace.id}


@router.websocket("/ws/ingest")
async def ws_ingest(websocket: WebSocket, api_key: str | None = Query(default=None)):
    """Streaming ingestion (API-key authed). Each message is a TraceIn JSON, or a WsEventsMessage."""
    org_id, _mode = await _authenticate_ws(None, api_key)
    if not org_id:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    try:
        async with async_session_maker() as db:
            org = await db.get(Organization, org_id)
        if org is None:
            await websocket.close(code=4401)
            return
        limit = org.rate_limit_per_min
        while True:
            raw = await websocket.receive_text()
            try:
                await enforce_rate_limit(f"org:{org_id}", limit)
            except HTTPException:
                await websocket.send_json({"ok": False, "error": "rate_limited"})
                continue
            # a session per message: one kept for the socket would serve rows other writers have since changed
            async with async_session_maker() as db:
                await websocket.send_json(await _ingest_message(db, org_id, raw))
    except WebSocketDisconnect:
        return
