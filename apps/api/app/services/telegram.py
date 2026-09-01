from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import Settings


class TelegramError(RuntimeError):
    pass


@dataclass(frozen=True)
class TelegramBotIdentity:
    id: int
    username: str | None


class TelegramNotificationService:
    """Outbound Bot API client. It deliberately never calls getUpdates."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _url(self, method: str) -> str:
        if not self._settings.telegram_bot_token:
            raise TelegramError("Telegram bot is not configured")
        return f"https://api.telegram.org/bot{self._settings.telegram_bot_token}/{method}"

    async def _call(self, method: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
                response = await client.post(self._url(method), json=body)
                response.raise_for_status()
                payload: dict[str, Any] = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TelegramError("Telegram API request failed") from exc
        if payload.get("ok") is not True:
            raise TelegramError("Telegram API rejected the request")
        return payload

    async def test_connection(self) -> TelegramBotIdentity:
        payload = await self._call("getMe", {})
        result = payload.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("id"), int):
            raise TelegramError("Telegram returned an invalid bot identity")
        return TelegramBotIdentity(id=result["id"], username=result.get("username"))

    async def send_message(self, text: str, reply_markup: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self._settings.telegram_chat_id:
            raise TelegramError("Telegram chat is not configured")
        body: dict[str, Any] = {
            "chat_id": self._settings.telegram_chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            body["reply_markup"] = reply_markup
        return await self._call("sendMessage", body)

    async def send_trade_approval_request(self, reference_id: str, text: str) -> dict[str, Any]:
        """Send a future semi-automatic approval request; this never submits an order."""
        if len(reference_id) > 40:
            raise TelegramError("Approval reference is too long for Telegram callback data")
        return await self.send_message(
            text,
            {
                "inline_keyboard": [
                    [
                        {"text": "Approve", "callback_data": f"sentinel:approve:{reference_id}"},
                        {"text": "Reject", "callback_data": f"sentinel:reject:{reference_id}"},
                    ],
                    [{"text": "EMERGENCY STOP", "callback_data": "sentinel:emergency_stop"}],
                ]
            },
        )

    async def answer_callback(self, callback_query_id: str, text: str) -> None:
        await self._call("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})

    async def register_webhook(self) -> None:
        if not self._settings.telegram_webhook_url or not self._settings.telegram_webhook_secret:
            raise TelegramError("Telegram webhook URL and secret are not configured")
        await self._call(
            "setWebhook",
            {
                "url": self._settings.telegram_webhook_url,
                "secret_token": self._settings.telegram_webhook_secret,
                "allowed_updates": ["callback_query", "message"],
            },
        )
