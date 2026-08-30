from functools import lru_cache
from typing import Literal

from pydantic import AnyHttpUrl, Field, PostgresDsn, RedisDsn, field_validator
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
