from datetime import UTC, datetime

from fastapi import APIRouter
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import engine

router = APIRouter(prefix="/health", tags=["System"])


class DependencyHealth(BaseModel):
    status: str
    detail: str | None = None


class HealthResponse(BaseModel):
    status: str
    mode: str
    live_trading_enabled: bool
    timestamp: datetime
    database: DependencyHealth
    redis: DependencyHealth


async def _database_health() -> DependencyHealth:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return DependencyHealth(status="healthy")
    except Exception:
        return DependencyHealth(status="offline", detail="Database unavailable")


async def _redis_health() -> DependencyHealth:
    redis = Redis.from_url(str(get_settings().redis_url), decode_responses=True)
    try:
        await redis.ping()
        return DependencyHealth(status="healthy")
    except Exception:
        return DependencyHealth(status="offline", detail="Redis unavailable")
    finally:
        await redis.aclose()


@router.get("", response_model=HealthResponse)
async def health() -> HealthResponse:
    settings = get_settings()
    database, redis = await _database_health(), await _redis_health()
    is_healthy = database.status == "healthy" and redis.status == "healthy"
    return HealthResponse(
        status="healthy" if is_healthy else "degraded",
        mode=settings.application_mode,
        live_trading_enabled=settings.live_trading_enabled,
        timestamp=datetime.now(UTC),
        database=database,
        redis=redis,
    )
