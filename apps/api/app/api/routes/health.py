from datetime import UTC, datetime

from fastapi import APIRouter, Response, status
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


class LivenessResponse(BaseModel):
    status: str
    timestamp: datetime


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


async def readiness_report() -> HealthResponse:
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


@router.get("", response_model=HealthResponse)
async def health() -> HealthResponse:
    return await readiness_report()


@router.get("/ready", response_model=HealthResponse)
async def readiness(response: Response) -> HealthResponse:
    report = await readiness_report()
    if report.status != "healthy":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return report


@router.get("/live", response_model=LivenessResponse)
async def liveness() -> LivenessResponse:
    """Process-level probe; dependency readiness remains available from the main health endpoint."""
    return LivenessResponse(status="healthy", timestamp=datetime.now(UTC))
