from functools import lru_cache
from typing import Literal

from pydantic import AnyHttpUrl, Field, PostgresDsn, RedisDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed server-side configuration; secrets must never be returned by routes."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    web_origin: AnyHttpUrl = "http://localhost:3000"
    timezone: str = "Asia/Kolkata"
    application_mode: Literal["PAPER", "REPLAY", "LIVE"] = "PAPER"
    live_trading_enabled: bool = False
    auto_create_schema: bool = False
    log_level: str = "INFO"

    database_url: PostgresDsn
    redis_url: RedisDsn
    jwt_secret: str = Field(min_length=32)
    access_token_minutes: int = Field(default=15, ge=1, le=60)
    refresh_token_days: int = Field(default=14, ge=1, le=90)
    cookie_secure: bool = False
    max_failed_login_attempts: int = Field(default=5, ge=3, le=20)
    login_lockout_minutes: int = Field(default=15, ge=1, le=1440)
    login_rate_limit_attempts: int = Field(default=10, ge=3, le=100)
    login_rate_limit_minutes: int = Field(default=15, ge=1, le=1440)

    firstock_api_key: str | None = None
    firstock_vendor_code: str | None = None
    firstock_user_id: str | None = None
    firstock_password: str | None = None
    firstock_totp_secret: str | None = None
    firstock_subscriptions: str = ""
    nifty_benchmark_token: str = "NSE:26000"

    upstox_access_token: str | None = None
    upstox_api_key: str | None = None
    upstox_api_secret: str | None = None
    upstox_redirect_uri: str | None = None
    upstox_token_encryption_key: str | None = None
    upstox_subscriptions: str = ""
    upstox_nifty_benchmark_key: str = "NSE_INDEX|Nifty 50"
    candle_timeframe_seconds: int = Field(default=60, ge=60, le=900)
    opening_range_minutes: int = Field(default=15, ge=5, le=60)
    ema_fast_period: int = Field(default=9, ge=2, le=100)
    ema_slow_period: int = Field(default=21, ge=3, le=200)
    volume_lookback_candles: int = Field(default=20, ge=3, le=200)
    upstox_instrument_refresh_hours: int = Field(default=24, ge=1, le=168)

    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_webhook_secret: str | None = None
    telegram_webhook_url: str | None = None
    telegram_allowed_user_ids: str = ""
    telegram_alert_cooldown_seconds: int = Field(default=900, ge=60, le=86400)
    telegram_config_encryption_key: str | None = None

    @property
    def firstock_is_configured(self) -> bool:
        return bool(
            self.firstock_api_key and self.firstock_vendor_code and self.firstock_user_id and self.firstock_password
        )

    @property
    def upstox_is_configured(self) -> bool:
        return bool(self.upstox_access_token and self.upstox_subscriptions)

    @property
    def upstox_oauth_is_configured(self) -> bool:
        return bool(
            self.upstox_api_key
            and self.upstox_api_secret
            and self.upstox_redirect_uri
            and self.upstox_token_encryption_key
        )

    @property
    def telegram_is_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def telegram_allowed_users(self) -> set[int]:
        return {int(value.strip()) for value in self.telegram_allowed_user_ids.split(",") if value.strip().isdigit()}

    @model_validator(mode="after")
    def require_sensible_indicator_periods(self) -> "Settings":
        if self.ema_fast_period >= self.ema_slow_period:
            raise ValueError("EMA_FAST_PERIOD must be lower than EMA_SLOW_PERIOD")
        return self

    @field_validator("timezone")
    @classmethod
    def require_market_timezone(cls, value: str) -> str:
        if value != "Asia/Kolkata":
            raise ValueError("Trading calculations must use Asia/Kolkata")
        return value

    @field_validator("application_mode")
    @classmethod
    def prohibit_implicit_live_mode(cls, value: str) -> str:
        if value == "LIVE":
            raise ValueError("LIVE application mode is not available in Release 1")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
