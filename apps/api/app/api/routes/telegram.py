import contextlib
import hmac
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import select

from app.api.deps import AppSettings, CurrentUser, DbSession, require_roles
from app.db.models import AuditLog, TelegramAlert, TelegramInboundEvent, TradeApprovalIntent, User, UserRole
from app.services.assisted_trading import decide_approval
from app.services.safety import emergency_stop
from app.services.telegram import TelegramError, TelegramNotificationService
from app.services.telegram_config import save_telegram_config

router = APIRouter(prefix="/telegram", tags=["Telegram"])


class TelegramStatus(BaseModel):
    configured: bool
    webhook_configured: bool
    inbound_enabled: bool
    detail: str


class TelegramConfigRequest(BaseModel):
    bot_token: str
    chat_id: str


def get_telegram_status(settings: AppSettings) -> TelegramStatus:
    webhook_ready = bool(settings.telegram_webhook_url and settings.telegram_webhook_secret)
    inbound_ready = settings.telegram_is_configured and webhook_ready and bool(settings.telegram_allowed_users)
    return TelegramStatus(
        configured=settings.telegram_is_configured,
        webhook_configured=webhook_ready,
        inbound_enabled=inbound_ready,
        detail="Dedicated bot is ready for configuration"
        if not settings.telegram_is_configured
        else "Outbound notifications are configured",
    )


async def _save_alert(
    session: DbSession, alert_type: str, chat_id: str, payload: dict, success: bool, message_id: str | None = None
) -> None:
    session.add(
        TelegramAlert(
            alert_type=alert_type,
            chat_id=chat_id,
            status="SENT" if success else "FAILED",
            telegram_message_id=message_id,
            payload=payload,
            failure_detail=None if success else "Telegram API request failed",
        )
    )
    await session.commit()


@router.get("/status", response_model=TelegramStatus)
async def telegram_status(settings: AppSettings, _: CurrentUser) -> TelegramStatus:
    return get_telegram_status(settings)


@router.put("/configuration", status_code=status.HTTP_204_NO_CONTENT)
async def configure_telegram(
    payload: TelegramConfigRequest, settings: AppSettings, user: User = Depends(require_roles(UserRole.ADMIN))
) -> None:
    try:
        await save_telegram_config(settings, payload.bot_token, payload.chat_id, user.id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/test", response_model=TelegramStatus)
async def test_telegram(
    settings: AppSettings, session: DbSession, user: User = Depends(require_roles(UserRole.ADMIN))
) -> TelegramStatus:
    if not settings.telegram_is_configured:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Telegram bot token and chat ID are required")
    service = TelegramNotificationService(settings)
    try:
        identity = await service.test_connection()
        result = await service.send_message(
            "✅ Intraday Sentinel Telegram connection test. PAPER mode is active; no order can be placed."
        )
        await _save_alert(
            session,
            "SYSTEM_TEST",
            settings.telegram_chat_id or "",
            {"bot_id": identity.id, "username": identity.username},
            True,
            str(result.get("result", {}).get("message_id", "")),
        )
    except TelegramError as exc:
        await _save_alert(session, "SYSTEM_TEST", settings.telegram_chat_id or "", {}, False)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Telegram connection test failed") from exc
    session.add(AuditLog(user_id=user.id, event_type="telegram.connection_test", metadata_json={"bot_id": identity.id}))
    await session.commit()
    return get_telegram_status(settings)


@router.post("/webhook/register", response_model=TelegramStatus)
async def register_webhook(
    settings: AppSettings, user: User = Depends(require_roles(UserRole.ADMIN))
) -> TelegramStatus:
    try:
        await TelegramNotificationService(settings).register_webhook()
    except TelegramError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Telegram webhook registration failed"
        ) from exc
    # Audit with an isolated session because this action has no request DB dependency.
    from app.db.session import SessionLocal

    async with SessionLocal() as audit_session:
        audit_session.add(
            AuditLog(user_id=user.id, event_type="telegram.webhook_registered", metadata_json={"url_configured": True})
        )
        await audit_session.commit()
    return get_telegram_status(settings)


def _callback_parts(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    callback = payload.get("callback_query")
    if not isinstance(callback, dict):
        return None, None
    data = callback.get("data")
    callback_id = callback.get("id")
    return data if isinstance(data, str) else None, callback_id if isinstance(callback_id, str) else None


@router.post("/webhook", status_code=status.HTTP_200_OK)
async def inbound_webhook(
    payload: dict[str, Any],
    request: Request,
    session: DbSession,
    settings: AppSettings,
    telegram_secret: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
) -> dict[str, bool]:
    if (
        not settings.telegram_webhook_secret
        or not telegram_secret
        or not hmac.compare_digest(settings.telegram_webhook_secret, telegram_secret)
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Telegram webhook secret")
    update_id = payload.get("update_id")
    if not isinstance(update_id, int):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Telegram update")
    if await session.scalar(select(TelegramInboundEvent).where(TelegramInboundEvent.telegram_update_id == update_id)):
        return {"ok": True}
    callback = payload.get("callback_query") if isinstance(payload.get("callback_query"), dict) else {}
    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    sender = (
        callback.get("from")
        if isinstance(callback.get("from"), dict)
        else message.get("from")
        if isinstance(message.get("from"), dict)
        else {}
    )
    callback_message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
    chat = (
        callback_message.get("chat")
        if isinstance(callback_message.get("chat"), dict)
        else message.get("chat")
        if isinstance(message.get("chat"), dict)
        else {}
    )
    sender_id = str(sender.get("id")) if sender.get("id") is not None else None
    chat_id = str(chat.get("id")) if chat.get("id") is not None else None
    accepted = bool(
        sender_id
        and sender_id.isdigit()
        and int(sender_id) in settings.telegram_allowed_users
        and chat_id == settings.telegram_chat_id
    )
    event_type = "callback_query" if callback else "message" if message else "unsupported"
    session.add(
        TelegramInboundEvent(
            telegram_update_id=update_id,
            event_type=event_type,
            sender_id=sender_id,
            chat_id=chat_id,
            accepted=accepted,
            payload=payload,
        )
    )
    callback_data, callback_id = _callback_parts(payload)
    response_text = "Command rejected"
    if accepted and callback_data:
        parts = callback_data.split(":")
        if (
            len(parts) == 3
            and parts[0] == "sentinel"
            and parts[1] in {"approve", "reject"}
            and 1 <= len(parts[2]) <= 40
        ):
            reference_id = parts[2]
            existing = await session.scalar(
                select(TradeApprovalIntent).where(TradeApprovalIntent.reference_id == reference_id)
            )
            if existing is None:
                existing = TradeApprovalIntent(
                    reference_id=reference_id,
                    decision="PENDING",
                    source="TELEGRAM",
                    requester_id=sender_id,
                    status="PENDING",
                    expires_at=datetime.now(UTC) + timedelta(minutes=5),
                )
                session.add(existing)
                await session.flush()
            else:
                existing.requester_id = sender_id
            await decide_approval(session, existing, "APPROVE" if parts[1] == "approve" else "REJECT")
            session.add(
                AuditLog(
                    event_type="telegram.trade_approval_intent",
                    metadata_json={"reference_id": reference_id, "decision": parts[1], "sender_id": sender_id},
                )
            )
            response_text = "Approval recorded. No order will be sent in this release."
        elif callback_data == "sentinel:emergency_stop":
            redis = Redis.from_url(str(settings.redis_url), decode_responses=True)
            try:
                await emergency_stop(redis, "Telegram emergency-stop callback", "telegram")
            finally:
                await redis.aclose()
            session.add(AuditLog(event_type="telegram.emergency_stop", metadata_json={"sender_id": sender_id}))
            response_text = "Emergency stop engaged. Scanner has been stopped."
    elif (
        accepted
        and isinstance(message.get("text"), str)
        and message["text"].strip().lower() in {"/stop", "/emergency_stop"}
    ):
        redis = Redis.from_url(str(settings.redis_url), decode_responses=True)
        try:
            await emergency_stop(redis, "Telegram emergency-stop command", "telegram")
        finally:
            await redis.aclose()
        session.add(AuditLog(event_type="telegram.emergency_stop", metadata_json={"sender_id": sender_id}))
        response_text = "Emergency stop engaged. Scanner has been stopped."
    await session.commit()
    if callback_id:
        with contextlib.suppress(TelegramError):
            await TelegramNotificationService(settings).answer_callback(callback_id, response_text)
    return {"ok": True}
