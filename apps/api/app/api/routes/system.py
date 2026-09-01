from datetime import UTC, datetime

from fastapi import APIRouter
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import text

from app.api.deps import CurrentUser
from app.api.routes.scanner import get_scanner_status
from app.core.config import get_settings
from app.db.session import engine
from app.services.firstock.market_data import CONNECTION_STATE_KEY
from app.services.trading_calendar import TradingCalendar

router = APIRouter(prefix="/system", tags=["System"])


class VersionResponse(BaseModel):
    application: str
    version: str
    mode: str
    live_trading_enabled: bool


class ServiceStatus(BaseModel):
    status: str
    detail: str
    checked_at: datetime


class SystemOverview(BaseModel):
    mode: str
    live_trading_enabled: bool
    api: ServiceStatus
    database: ServiceStatus
    redis: ServiceStatus
    scanner: ServiceStatus
    market_data: ServiceStatus
    firstock: ServiceStatus
    telegram: ServiceStatus


class MarketSessionStatus(BaseModel):
    phase: str
    trading_day: bool
    reason: str
    local_timestamp: datetime
    session_date: str | None
    regular_open: str | None
    regular_close: str | None
    is_special_session: bool


@router.get("/version", response_model=VersionResponse)
async def version() -> VersionResponse:
    settings = get_settings()
    return VersionResponse(
        application="Intraday Sentinel",
        version="0.1.0",
        mode=settings.application_mode,
        live_trading_enabled=settings.live_trading_enabled,
    )


@router.get("/market-session", response_model=MarketSessionStatus)
async def market_session(_: CurrentUser, at: datetime | None = None) -> MarketSessionStatus:
    settings = get_settings()
    calendar = TradingCalendar.from_settings(settings)
    status = calendar.status_at(at or datetime.now(UTC))
    return MarketSessionStatus.model_validate(status.as_dict())


@router.get("/overview", response_model=SystemOverview)
async def overview(_: CurrentUser) -> SystemOverview:
    settings = get_settings()
    checked_at = datetime.now(UTC)
    database = ServiceStatus(status="offline", detail="Database unavailable", checked_at=checked_at)
    redis_status = ServiceStatus(status="offline", detail="Redis unavailable", checked_at=checked_at)
    scanner_status = ServiceStatus(status="offline", detail="Redis unavailable", checked_at=checked_at)
    market_data = ServiceStatus(
        status="not_configured",
        detail="Configure the selected market-data connector before starting the scanner",
        checked_at=checked_at,
    )
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        database = ServiceStatus(status="healthy", detail="PostgreSQL connection verified", checked_at=checked_at)
    except Exception:
        pass
    redis = Redis.from_url(str(settings.redis_url), decode_responses=True)
    firstock = ServiceStatus(
        status="disconnected" if settings.firstock_is_configured else "not_configured",
        detail="Credentials configured; scanner is stopped"
        if settings.firstock_is_configured
        else "Set server-side Firstock credentials to enable the market feed",
        checked_at=checked_at,
    )
    try:
        await redis.ping()
        redis_status = ServiceStatus(status="healthy", detail="Redis connection verified", checked_at=checked_at)
        scanner = await get_scanner_status(redis)
        scanner_status = ServiceStatus(status=scanner.status.lower(), detail=scanner.detail, checked_at=checked_at)
        market_data_state = await redis.hgetall(CONNECTION_STATE_KEY)
        if market_data_state:
            market_data = ServiceStatus(
                status=market_data_state.get("status", "unknown").lower(),
                detail=market_data_state.get("detail", "No connection detail"),
                checked_at=checked_at,
            )
    except Exception:
        pass
    finally:
        await redis.aclose()
    return SystemOverview(
        mode=settings.application_mode,
        live_trading_enabled=settings.live_trading_enabled,
        api=ServiceStatus(status="healthy", detail="FastAPI service is accepting requests", checked_at=checked_at),
        database=database,
        redis=redis_status,
        scanner=scanner_status,
        market_data=market_data,
        firstock=firstock,
        telegram=ServiceStatus(
            status="configured" if settings.telegram_is_configured else "not_configured",
            detail=(
                "Dedicated-bot alerts are configured"
                if settings.telegram_is_configured
                else "Set a dedicated Telegram bot token and chat ID to enable alerts"
            ),
            checked_at=checked_at,
        ),
    )
