from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from app.api.deps import AppSettings, CurrentUser, require_roles
from app.db.models import AuditLog, User, UserRole
from app.db.session import SessionLocal
from app.services.safety import (
    PAPER_TRACKING_KEY,
    clear_emergency_stop,
    emergency_stop,
    emergency_stop_state,
    paper_tracking_enabled,
)

router = APIRouter(prefix="/safety", tags=["Safety"])


class SafetyStatus(BaseModel):
    paper_tracking_enabled: bool
    live_trading_enabled: bool
    live_execution_available: bool
    emergency_stop_active: bool
    emergency_stop_reason: str | None = None
    emergency_stop_source: str | None = None
    emergency_stop_at: datetime | None = None


class EmergencyStopRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=250)


async def _redis(settings: AppSettings) -> Redis:
    return Redis.from_url(str(settings.redis_url), decode_responses=True)


async def get_safety_status(settings: AppSettings) -> SafetyStatus:
    redis = await _redis(settings)
    try:
        stopped = await emergency_stop_state(redis)
        paper_enabled = await paper_tracking_enabled(redis)
    finally:
        await redis.aclose()
    return SafetyStatus(
        paper_tracking_enabled=paper_enabled,
        live_trading_enabled=False,
        live_execution_available=False,
        emergency_stop_active=stopped.get("active") == "true",
        emergency_stop_reason=stopped.get("reason"),
        emergency_stop_source=stopped.get("source"),
        emergency_stop_at=datetime.fromisoformat(stopped["at"]) if stopped.get("at") else None,
    )


async def _audit(user: User, event: str, metadata: dict) -> None:
    async with SessionLocal() as session:
        session.add(AuditLog(user_id=user.id, event_type=event, metadata_json=metadata))
        await session.commit()


@router.get("/status", response_model=SafetyStatus)
async def safety_status(settings: AppSettings, _: CurrentUser) -> SafetyStatus:
    return await get_safety_status(settings)


@router.post("/paper/enable", response_model=SafetyStatus)
async def enable_paper(settings: AppSettings, user: User = Depends(require_roles(UserRole.ADMIN))) -> SafetyStatus:
    redis = await _redis(settings)
    try:
        await redis.set(PAPER_TRACKING_KEY, "true")
    finally:
        await redis.aclose()
    await _audit(user, "safety.paper_tracking_enabled", {})
    return await get_safety_status(settings)


@router.post("/paper/disable", response_model=SafetyStatus)
async def disable_paper(settings: AppSettings, user: User = Depends(require_roles(UserRole.ADMIN))) -> SafetyStatus:
    redis = await _redis(settings)
    try:
        await redis.set(PAPER_TRACKING_KEY, "false")
    finally:
        await redis.aclose()
    await _audit(user, "safety.paper_tracking_disabled", {})
    return await get_safety_status(settings)


@router.post("/emergency-stop", response_model=SafetyStatus)
async def engage_emergency_stop(
    payload: EmergencyStopRequest,
    settings: AppSettings,
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.TRADER)),
) -> SafetyStatus:
    redis = await _redis(settings)
    try:
        await emergency_stop(redis, payload.reason, "web")
    finally:
        await redis.aclose()
    await _audit(user, "safety.emergency_stop_engaged", {"reason": payload.reason})
    return await get_safety_status(settings)


@router.post("/emergency-stop/clear", response_model=SafetyStatus)
async def clear_stop(settings: AppSettings, user: User = Depends(require_roles(UserRole.ADMIN))) -> SafetyStatus:
    redis = await _redis(settings)
    try:
        await clear_emergency_stop(redis)
    finally:
        await redis.aclose()
    await _audit(user, "safety.emergency_stop_cleared", {})
    return await get_safety_status(settings)


@router.post("/live/enable", response_model=SafetyStatus)
async def rejected_live_enable(
    _: EmergencyStopRequest, __: User = Depends(require_roles(UserRole.ADMIN))
) -> SafetyStatus:
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT, detail="Live execution is unavailable in Release 1; no order path exists"
    )
