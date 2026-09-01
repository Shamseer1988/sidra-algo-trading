"""Encrypted server-side Telegram configuration; tokens are never returned to clients."""

import json

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import Settings
from app.db.models import BrokerCredential
from app.db.session import SessionLocal

PROVIDER = "TELEGRAM"


def _fernet(settings: Settings) -> Fernet:
    if not settings.telegram_config_encryption_key:
        raise ValueError("TELEGRAM_CONFIG_ENCRYPTION_KEY is required")
    return Fernet(settings.telegram_config_encryption_key.encode("utf-8"))


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
    if not settings.telegram_config_encryption_key:
        return settings
    async with SessionLocal() as session:
        item = await session.get(BrokerCredential, PROVIDER)
    if item is None:
        return settings
    try:
        value = json.loads(_fernet(settings).decrypt(item.encrypted_access_token.encode()))
        return settings.model_copy(
            update={"telegram_bot_token": value["bot_token"], "telegram_chat_id": value["chat_id"]}
        )
    except (InvalidToken, ValueError, KeyError, json.JSONDecodeError):
        return settings
