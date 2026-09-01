"""Authenticated scanner event fan-out for the protected terminal."""

import asyncio
import json

import jwt
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from redis.asyncio import Redis

from app.core.config import get_settings
from app.services.auth import decode_access_token

router = APIRouter(tags=["Events"])
SCANNER_EVENTS_CHANNEL = "scanner:events"


@router.websocket("/events/scanner")
async def scanner_events(websocket: WebSocket) -> None:
    """Stream paper-signal notices only after validating the HttpOnly session cookie."""
    settings = get_settings()
    token = websocket.cookies.get("access_token")
    try:
        if not token:
            raise jwt.InvalidTokenError("missing session")
        decode_access_token(token, settings)
    except jwt.InvalidTokenError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await websocket.accept()
    redis = Redis.from_url(str(settings.redis_url), decode_responses=True)
    pubsub = redis.pubsub()
    await pubsub.subscribe(SCANNER_EVENTS_CHANNEL)
    try:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=15.0)
            if message and isinstance(message.get("data"), str):
                await websocket.send_text(message["data"])
            else:
                await websocket.send_text(json.dumps({"type": "heartbeat"}))
            await asyncio.sleep(0)
    except WebSocketDisconnect:
        pass
    finally:
        await pubsub.unsubscribe(SCANNER_EVENTS_CHANNEL)
        await pubsub.aclose()
        await redis.aclose()
