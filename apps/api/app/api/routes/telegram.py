import contextlib
import hmac
import html
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import select

from app.api.deps import AppSettings, CurrentUser, DbSession, require_roles
from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY, TradingControls
from app.db.models import (
    ApplicationSetting,
    AuditLog,
    TelegramAlert,
    TelegramInboundEvent,
    TradeApprovalIntent,
    User,
    UserRole,
)
from app.services.assisted_trading import decide_approval
from app.services.live_approval import APPROVE_ACTION, CALLBACK_PREFIX, REJECT_ACTION, decide_live_approval
from app.services.live_execution_gateway import live_order_adapter
from app.services.safety import emergency_stop
from app.services.telegram import TelegramError, TelegramNotificationService
from app.services.telegram_config import save_telegram_config

logger = logging.getLogger(__name__)

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


@dataclass(frozen=True)
class CallbackReply:
    """What to tell the operator, and how loudly.

    The kind is carried rather than inferred from the text, because the heading
    is the part read first and a cheerful tick over a refusal is worse than no
    reply at all -- which is the state this whole reply exists to end.
    """

    kind: str
    text: str


SUBMITTED_KIND = "submitted"
REJECTED_KIND = "rejected"
BLOCKED_KIND = "blocked"
ERROR_KIND = "error"
INFO_KIND = "info"
STOPPED_KIND = "stopped"

# Heading per kind. Deliberately blunt: the operator is reading this on a phone,
# possibly in a meeting, and needs the outcome in one word.
_REPLY_HEADINGS = {
    SUBMITTED_KIND: "\u2705 <b>APPROVED — ORDER SENT</b>",
    REJECTED_KIND: "\u26d4 <b>REJECTED</b>",
    BLOCKED_KIND: "\U0001f6ab <b>NOT SENT</b>",
    ERROR_KIND: "\u26a0\ufe0f <b>ERROR</b>",
    INFO_KIND: "\u2139\ufe0f <b>RECORDED</b>",
    STOPPED_KIND: "\U0001f6d1 <b>EMERGENCY STOP</b>",
}

# decide_live_approval's vocabulary, mapped to how it should read.
_LIVE_STATUS_KINDS = {
    "SUBMITTED": SUBMITTED_KIND,
    "APPROVED": SUBMITTED_KIND,
    "REJECTED": REJECTED_KIND,
    "EXPIRED": BLOCKED_KIND,
    "BLOCKED": BLOCKED_KIND,
}


def _reply_message(reply: CallbackReply) -> str:
    heading = _REPLY_HEADINGS.get(reply.kind, _REPLY_HEADINGS[INFO_KIND])
    return f"{heading}\n\n{html.escape(reply.text)}"


async def _handle_live_decision(
    session: DbSession,
    settings: AppSettings,
    *,
    reference_id: str,
    action: str,
    decided_by: str | None,
) -> CallbackReply:
    """Act on a live approval tap, and never let a failure read as success.

    Every failure path here returns a message saying nothing was sent, because
    the operator's next action depends on believing the answer. A silent error
    would leave them assuming an order exists, or assuming one does not.
    """
    trading = await session.get(ApplicationSetting, TRADING_KEY)
    controls = TradingControls.model_validate(trading.value if trading else DEFAULT_TRADING_CONTROLS)

    try:
        adapter = await live_order_adapter(settings, session, controls.live_broker)
    except Exception as exc:  # no broker selected, unreachable, credentials missing
        return CallbackReply(ERROR_KIND, f"Could not reach the broker; nothing was sent. ({exc})")

    redis = Redis.from_url(str(settings.redis_url), decode_responses=True)
    try:
        result = await decide_live_approval(
            session,
            settings,
            adapter,
            redis,
            reference_id=reference_id,
            action=action,
            decided_by=decided_by,
            approval_mode=controls.execution_approval_mode,
        )
    except Exception:
        logger.exception("Live approval decision failed for %s", reference_id)
        return CallbackReply(ERROR_KIND, "The decision could not be completed. Check the order book before retrying.")
    finally:
        await redis.aclose()
    return CallbackReply(_LIVE_STATUS_KINDS.get(result.status, BLOCKED_KIND), result.detail)


async def _deliver_reply(
    notifier: TelegramNotificationService,
    callback_id: str | None,
    reply: CallbackReply,
    *,
    announce: bool,
) -> None:
    """Answer the tap twice, and never fail the webhook for it.

    ``announce`` is false for a sender who is not on the allow list. The toast
    goes back to whoever tapped, but the chat message goes to the operator's
    chat -- so announcing a refusal would let anyone who found the bot post into
    it at will. They get told no; the operator is not made to read it.

    answerCallbackQuery clears the spinner on the button, but it is a toast
    lasting a second or two: an operator who looked away has no way to find out
    what their tap did. The chat message is the durable record they can scroll
    back to, and it is what makes an approval feel answered rather than
    swallowed.

    Both are suppressed rather than raised. Telegram retries a webhook that did
    not return 200, and a retry re-enters the handler -- so an error raised here,
    after the decision is already committed, would risk the operator's answer
    being processed twice over a failure that was only cosmetic.
    """
    if callback_id:
        with contextlib.suppress(TelegramError):
            # Telegram truncates this hard; the chat message carries the full text.
            await notifier.answer_callback(callback_id, reply.text[:200])
    if not announce:
        return
    with contextlib.suppress(TelegramError):
        await notifier.send_message(_reply_message(reply), parse_mode="HTML")


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
    reply = CallbackReply(ERROR_KIND, "Command rejected: this sender is not on the allow list.")
    if accepted and callback_data:
        parts = callback_data.split(":")
        # Live approvals carry their own prefix so that a live decision can never
        # be handled by the paper branch, or the reverse, however either evolves.
        if (
            len(parts) == 3
            and parts[0] == CALLBACK_PREFIX
            and parts[1] in {APPROVE_ACTION, REJECT_ACTION}
            and 1 <= len(parts[2]) <= 40
        ):
            reply = await _handle_live_decision(
                session, settings, reference_id=parts[2], action=parts[1], decided_by=sender_id
            )
            session.add(
                AuditLog(
                    event_type="telegram.live_order_decision",
                    metadata_json={"reference_id": parts[2], "action": parts[1], "sender_id": sender_id},
                )
            )
        elif (
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
            reply = CallbackReply(
                INFO_KIND,
                "Paper signal "
                + ("approved" if parts[1] == "approve" else "rejected")
                + ". This is the paper journal; no live order is sent from this message.",
            )
        elif callback_data == "sentinel:emergency_stop":
            redis = Redis.from_url(str(settings.redis_url), decode_responses=True)
            try:
                await emergency_stop(redis, "Telegram emergency-stop callback", "telegram")
            finally:
                await redis.aclose()
            session.add(AuditLog(event_type="telegram.emergency_stop", metadata_json={"sender_id": sender_id}))
            reply = CallbackReply(STOPPED_KIND, "Emergency stop engaged. The scanner has been stopped.")
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
        reply = CallbackReply(STOPPED_KIND, "Emergency stop engaged. The scanner has been stopped.")
    await session.commit()

    # Two deliveries, deliberately. answerCallbackQuery clears the spinner on the
    # button but is a toast that lasts a second or two; an operator who looked
    # away has no way to find out what their tap did. The chat message is the
    # durable record they can scroll back to, and it is what makes an approval
    # feel answered rather than swallowed.
    await _deliver_reply(TelegramNotificationService(settings), callback_id, reply, announce=bool(accepted))
    return {"ok": True}
