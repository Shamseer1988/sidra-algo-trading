from datetime import date, time
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
    live_compliance_approved: bool = False
    live_static_ip_verified: bool = False
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
    upstox_mobile_number: str | None = None
    upstox_pin: str | None = None
    upstox_totp_secret: str | None = None
    upstox_auto_auth_enabled: bool = False
    upstox_subscriptions: str = ""
    upstox_nifty_benchmark_key: str = "NSE_INDEX|Nifty 50"
    candle_timeframe_seconds: int = Field(default=60, ge=60, le=900)
    opening_range_minutes: int = Field(default=15, ge=5, le=60)
    ema_fast_period: int = Field(default=9, ge=2, le=100)
    ema_slow_period: int = Field(default=21, ge=3, le=200)
    volume_lookback_candles: int = Field(default=20, ge=3, le=200)
    atr_period: int = Field(default=14, ge=2, le=100)
    daily_history_sessions: int = Field(default=40, ge=5, le=250)
    rvol_baseline_sessions: int = Field(default=10, ge=1, le=60)
    backtest_sweep_max_combinations: int = Field(default=40, ge=2, le=200)

    # Dynamic scan universe. Opt-in: while disabled the scanner evaluates every streamed instrument.
    universe_enabled: bool = False
    universe_size: int = Field(default=30, ge=1, le=200)
    universe_refresh_time: str = Field(default="09:25", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    universe_min_avg_turnover: float = Field(default=250_000_000, ge=0)
    universe_min_price: float = Field(default=40, ge=0)
    universe_max_price: float = Field(default=15_000, ge=0)
    universe_min_atr_percent: float = Field(default=0.8, ge=0, le=50)
    universe_max_atr_percent: float = Field(default=8.0, ge=0, le=50)

    # Market regime. Opt-in: while disabled the scanner uses only the intraday NIFTY EMA/VWAP regime.
    market_regime_enabled: bool = False
    upstox_india_vix_key: str = "NSE_INDEX|India VIX"
    india_vix_calm_below: float = Field(default=13.0, ge=0, le=100)
    india_vix_stressed_above: float = Field(default=18.0, ge=0, le=100)
    india_vix_extreme_above: float = Field(default=25.0, ge=0, le=100)
    upstox_instrument_refresh_hours: int = Field(default=24, ge=1, le=168)
    nse_calendar_confirmed_years: str = "2026"
    nse_holiday_overrides: str = ""
    nse_special_sessions: str = ""
    data_quality_stale_after_seconds: int = Field(default=20, ge=5, le=300)
    data_quality_max_tick_latency_ms: int = Field(default=5000, ge=100, le=60000)
    data_quality_max_missing_bars: int = Field(default=0, ge=0, le=30)
    data_quality_max_duplicate_percent: float = Field(default=10, ge=0, le=100)

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
    def upstox_auto_auth_is_configured(self) -> bool:
        return bool(
            self.upstox_auto_auth_enabled
            and self.upstox_mobile_number
            and self.upstox_pin
            and self.upstox_totp_secret
            and self.upstox_api_key
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
        if self.live_trading_enabled:
            raise ValueError("LIVE_TRADING_ENABLED must remain false until live activation gates exist")
        if not self.calendar_confirmed_years:
            raise ValueError("NSE_CALENDAR_CONFIRMED_YEARS must contain at least one year")
        if self.universe_min_price >= self.universe_max_price:
            raise ValueError("UNIVERSE_MIN_PRICE must be lower than UNIVERSE_MAX_PRICE")
        if self.universe_min_atr_percent >= self.universe_max_atr_percent:
            raise ValueError("UNIVERSE_MIN_ATR_PERCENT must be lower than UNIVERSE_MAX_ATR_PERCENT")
        if not self.india_vix_calm_below < self.india_vix_stressed_above < self.india_vix_extreme_above:
            raise ValueError("India VIX thresholds must satisfy calm < stressed < extreme")
        try:
            for value in self.nse_holiday_overrides.split(","):
                if value.strip():
                    date.fromisoformat(value.strip())
            for value in self.nse_special_sessions.split(","):
                if not value.strip():
                    continue
                date_part, hours = value.strip().split("@", 1)
                open_part, close_part = hours.split("-", 1)
                date.fromisoformat(date_part)
                if time.fromisoformat(open_part) >= time.fromisoformat(close_part):
                    raise ValueError("special-session close must be later than open")
        except ValueError as exc:
            raise ValueError("Invalid NSE holiday or special-session configuration") from exc
        if self.app_env == "production":
            if not self.cookie_secure:
                raise ValueError("COOKIE_SECURE must be true in production")
            if self.auto_create_schema:
                raise ValueError("AUTO_CREATE_SCHEMA must be false in production")
            if self.web_origin.scheme != "https":
                raise ValueError("WEB_ORIGIN must use HTTPS in production")
            if self.jwt_secret.lower().startswith(("change-this", "generate-a-")):
                raise ValueError("JWT_SECRET must be replaced before production startup")
        return self

    @property
    def calendar_confirmed_years(self) -> set[int]:
        return {int(value.strip()) for value in self.nse_calendar_confirmed_years.split(",") if value.strip().isdigit()}

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
