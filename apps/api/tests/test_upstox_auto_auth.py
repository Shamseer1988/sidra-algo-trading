from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pyotp
import pytest

from app.core.config import Settings
from app.services.upstox_auto_auth import UpstoxAutoAuthService


@pytest.fixture
def sample_settings() -> Settings:
    totp_secret = pyotp.random_base32()
    return Settings(
        database_url="postgresql+asyncpg://sentinel:sentinel@localhost:5432/sentinel",
        redis_url="redis://localhost:6379/0",
        jwt_secret="x" * 32,
        upstox_api_key="mock_api_key",
        upstox_api_secret="mock_api_secret",
        upstox_redirect_uri="http://localhost:3000/callback",
        upstox_token_encryption_key="dGhpcy1pcy1hLTMyLWJ5dGUtZmVybmV0LWtleS0xMjM0NTY=",
        upstox_mobile_number="9876543210",
        upstox_pin="123456",
        upstox_totp_secret=totp_secret,
        upstox_auto_auth_enabled=True,
        telegram_bot_token="mock_bot_token",
        telegram_chat_id="12345678",
    )


def test_generate_current_totp_produces_valid_6_digit_code(sample_settings: Settings):
    service = UpstoxAutoAuthService(sample_settings)
    totp_code = service.generate_current_totp()
    assert len(totp_code) == 6
    assert totp_code.isdigit()
    totp = pyotp.TOTP(sample_settings.upstox_totp_secret)
    assert totp.verify(totp_code)


@pytest.mark.asyncio
async def test_execute_automated_login_success_and_sends_telegram(sample_settings: Settings):
    service = UpstoxAutoAuthService(sample_settings)
    now = datetime.now(UTC)

    mock_send = AsyncMock()
    with (
        patch(
            "app.services.upstox_auto_auth.exchange_authorization_code",
            new_callable=AsyncMock,
            return_value="mock_access_token_1234567890",
        ) as mock_exchange,
        patch(
            "app.services.upstox_auto_auth.store_access_token",
            new_callable=AsyncMock,
            return_value=now,
        ) as mock_store,
        patch.object(service._telegram, "send_message", mock_send),
    ):
        result = await service.execute_automated_login(auth_code_override="manual_code_xyz")

        assert result.success is True
        assert "mock...7890" in result.token_masked
        assert result.expires_at == now
        mock_exchange.assert_awaited_once_with(sample_settings, "manual_code_xyz")
        mock_store.assert_awaited_once_with(sample_settings, "mock_access_token_1234567890", updated_by_user_id=None)
        mock_send.assert_awaited_once()
        assert "Upstox Auth Renewed" in mock_send.call_args[0][0]
