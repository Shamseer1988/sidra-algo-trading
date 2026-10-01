from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from app.api.deps import AppSettings, CurrentUser, DbSession, require_roles
from app.db.models import AuditLog, User, UserRole
from app.db.session import SessionLocal
from app.services.live_readiness import inspect_live_readiness
from app.services.safety import (
    PAPER_TRACKING_KEY,
    SCANNER_CONTROL_KEY,
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


class TradingStatusResponse(BaseModel):
    """One card's worth of state, decided on the server.

    Computed here rather than in the browser because the ordering between
    seven gates is a safety judgement -- which one to name when several are
    failing -- and a judgement made in a React component is one nobody can
    test or audit.
    """

    state: str
    headline: str
    detail: str
    remedy: str
    blocker: str | None
    trading: bool
    # Carried so the card can offer the right action without a second request.
    can_resume: bool
    can_pause: bool
    emergency_stop_active: bool


@router.get("/trading-status", response_model=TradingStatusResponse)
async def trading_status(_: CurrentUser, settings: AppSettings, session: DbSession) -> TradingStatusResponse:
    """What one card should say, and which button it should offer."""
    from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY, TradingControls
    from app.db.models import ApplicationSetting
    from app.services.trade_counter import count_filled_entries
    from app.services.trading_calendar import MARKET_TIMEZONE
    from app.services.trading_status import evaluate, summarise

    redis = await _redis(settings)
    try:
        stopped = await emergency_stop_state(redis)
        paper_enabled = await paper_tracking_enabled(redis)
        control_state = await redis.get(SCANNER_CONTROL_KEY) or "STOPPED"
        # The worker writes this every 30s with a 90s expiry, so its absence
        # means the loop is not running -- a different fault from being
        # switched off, and a different thing to do about it.
        heartbeat_fresh = await redis.get("scanner:heartbeat") is not None
    finally:
        await redis.aclose()

    row = await session.get(ApplicationSetting, TRADING_KEY)
    controls = TradingControls(**(row.value if row else DEFAULT_TRADING_CONTROLS))
    report = await inspect_live_readiness(session, settings)
    gates = {gate.key: gate.passed for gate in report.gates}
    reconciliation_detail = next(
        (gate.detail for gate in report.gates if gate.key == "external_reconciliation" and not gate.passed), ""
    )

    today = datetime.now(UTC).astimezone(MARKET_TIMEZONE).date()
    taken = await count_filled_entries(session, today)

    result = evaluate(
        application_mode=settings.application_mode,
        live_trading_enabled=settings.live_trading_enabled,
        emergency_stop_active=stopped.get("active") == "true",
        emergency_stop_reason=stopped.get("reason"),
        control_state=control_state,
        paper_tracking=paper_enabled,
        scanner_heartbeat_fresh=heartbeat_fresh,
        live_broker=controls.live_broker,
        approval_mode=controls.execution_approval_mode,
        armed=gates.get("administrator_activation", False),
        reconciliation_ok=gates.get("external_reconciliation", False),
        reconciliation_detail=reconciliation_detail,
        trades_used=taken.total,
        trades_ceiling=controls.maximum_daily_trades,
    )
    return TradingStatusResponse(
        **summarise(result),
        can_resume=result.blocker == "activation",
        can_pause=result.state == "TRADING",
        emergency_stop_active=stopped.get("active") == "true",
    )


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
