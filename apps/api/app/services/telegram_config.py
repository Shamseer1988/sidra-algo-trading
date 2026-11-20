"""Encrypted server-side Telegram configuration; tokens are never returned to clients.

Encrypted with ``TELEGRAM_CONFIG_ENCRYPTION_KEY`` when the server has one, and
otherwise with the server key that protects the Upstox token, so the bot can be
set up from the Connections screen without a second key in ``.env``. A value
saved under one key stays readable only while that key is the one in use.
"""

import json

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import Settings
from app.db.models import BrokerCredential
from app.db.session import SessionLocal

PROVIDER = "TELEGRAM"


def _key(settings: Settings) -> str | None:
    return settings.telegram_config_encryption_key or settings.upstox_token_encryption_key


def _fernet(settings: Settings) -> Fernet:
    key = _key(settings)
    if not key:
        raise ValueError("The server has no encryption key, so the Telegram bot cannot be stored safely")
    return Fernet(key.encode("utf-8"))


async def load_telegram_config(settings: Settings) -> dict[str, str]:
    """The saved bot token and chat id, decrypted; empty when nothing is saved or it cannot be read."""
    if not _key(settings):
        return {}
    async with SessionLocal() as session:
        item = await session.get(BrokerCredential, PROVIDER)
    if item is None:
        return {}
    try:
        value = json.loads(_fernet(settings).decrypt(item.encrypted_access_token.encode()))
    except (InvalidToken, ValueError, json.JSONDecodeError):
        return {}
    return {key: str(value[key]) for key in ("bot_token", "chat_id") if value.get(key)}


async def save_telegram_config(settings: Settings, bot_token: str, chat_id: str, user_id) -> None:
    encrypted = _fernet(settings).encrypt(json.dumps({"bot_token": bot_token, "chat_id": chat_id}).encode()).decode()
    async with SessionLocal() as session:
        item = await session.get(BrokerCredential, PROVIDER)
        if item is None:
            from datetime import UTC, datetime

            session.add(
                BrokerCredential(
                    provider=PROVIDER,
                    encrypted_access_token=encrypted,
                    expires_at=datetime.max.replace(tzinfo=UTC),
                    updated_by_user_id=user_id,
                )
            )
        else:
            item.encrypted_access_token, item.updated_by_user_id = encrypted, user_id
        await session.commit()


async def configured_settings(settings: Settings) -> Settings:
    saved = await load_telegram_config(settings)
    if not saved.get("bot_token") or not saved.get("chat_id"):
        return settings
    return settings.model_copy(update={"telegram_bot_token": saved["bot_token"], "telegram_chat_id": saved["chat_id"]})
