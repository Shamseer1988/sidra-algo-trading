from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from app.api.deps import AppSettings, CurrentUser, DbSession, require_roles
from app.db.models import AuditLog, User, UserRole
from app.db.session import SessionLocal
from app.services.live_readiness import inspect_live_readiness
from app.services.safety import (
    PAPER_TRACKING_KEY,
    clear_emergency_stop,
    emergency_stop,
    emergency_stop_state,
    paper_tracking_enabled,
)

router = APIRouter(prefix="/safety", tags=["Safety"])


class SafetyStatus(BaseModel):
    """What the operator's screen is entitled to claim about live execution.

    Every field here was pinned to a Release-1 constant while the environment
    itself refused to boot in live mode. It cannot be a constant now: the
    dashboard renders these directly, so a stale ``False`` is a screen telling
    somebody their money is not at risk while it is.
    """

    paper_tracking_enabled: bool
    application_mode: str
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


async def get_safety_status(settings: AppSettings, session: DbSession) -> SafetyStatus:
    redis = await _redis(settings)
    try:
        stopped = await emergency_stop_state(redis)
        paper_enabled = await paper_tracking_enabled(redis)
    finally:
        await redis.aclose()
    # overall_ready is the only honest source for "could an order reach the
    # broker right now": it requires every gate, the administrator activation
    # among them, so it goes false the moment the arming window lapses.
    report = await inspect_live_readiness(session, settings)
    return SafetyStatus(
        paper_tracking_enabled=paper_enabled,
        application_mode=settings.application_mode,
        live_trading_enabled=settings.live_trading_enabled,
        live_execution_available=report.overall_ready,
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
async def safety_status(settings: AppSettings, session: DbSession, _: CurrentUser) -> SafetyStatus:
    return await get_safety_status(settings, session)


@router.post("/paper/enable", response_model=SafetyStatus)
async def enable_paper(
    settings: AppSettings, session: DbSession, user: User = Depends(require_roles(UserRole.ADMIN))
) -> SafetyStatus:
    redis = await _redis(settings)
    try:
        await redis.set(PAPER_TRACKING_KEY, "true")
    finally:
        await redis.aclose()
    await _audit(user, "safety.paper_tracking_enabled", {})
    return await get_safety_status(settings, session)


@router.post("/paper/disable", response_model=SafetyStatus)
async def disable_paper(
    settings: AppSettings, session: DbSession, user: User = Depends(require_roles(UserRole.ADMIN))
) -> SafetyStatus:
    redis = await _redis(settings)
    try:
        await redis.set(PAPER_TRACKING_KEY, "false")
    finally:
        await redis.aclose()
    await _audit(user, "safety.paper_tracking_disabled", {})
    return await get_safety_status(settings, session)


@router.post("/emergency-stop", response_model=SafetyStatus)
async def engage_emergency_stop(
    payload: EmergencyStopRequest,
    settings: AppSettings,
    session: DbSession,
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.TRADER)),
) -> SafetyStatus:
    redis = await _redis(settings)
    try:
        await emergency_stop(redis, payload.reason, "web")
    finally:
        await redis.aclose()
    await _audit(user, "safety.emergency_stop_engaged", {"reason": payload.reason})
    return await get_safety_status(settings, session)


@router.post("/emergency-stop/clear", response_model=SafetyStatus)
async def clear_stop(
    settings: AppSettings, session: DbSession, user: User = Depends(require_roles(UserRole.ADMIN))
) -> SafetyStatus:
    redis = await _redis(settings)
    try:
        await clear_emergency_stop(redis)
    finally:
        await redis.aclose()
    await _audit(user, "safety.emergency_stop_cleared", {})
    return await get_safety_status(settings, session)


@router.post("/live/enable", response_model=SafetyStatus)
async def rejected_live_enable(
    _: EmergencyStopRequest, __: User = Depends(require_roles(UserRole.ADMIN))
) -> SafetyStatus:
    # An order path exists now, so the old message would be untrue. Live
    # submission is armed through POST /live-shadow/activation, which checks
    # every readiness gate and grants a window that expires on its own; a
    # boolean toggle with none of that is exactly what should not exist here.
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            "Live execution is not enabled from this endpoint. "
            "Arm it through POST /api/v1/live-shadow/activation, which requires every readiness gate to pass."
        ),
    )
