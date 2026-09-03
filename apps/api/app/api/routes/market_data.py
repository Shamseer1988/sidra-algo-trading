"""Protected configuration of paper-only market-data connectors."""

import json
import secrets
from datetime import UTC, date, datetime
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import select

from app.api.deps import AppSettings, CurrentUser, DbSession, require_roles
from app.db.models import AuditLog, MarketCandle, User, UserRole
from app.db.session import SessionLocal
from app.services.broker_controls import BrokerControls, load_broker_controls, save_broker_controls
from app.services.scheduler import AUTO_AUTH_STATUS_KEY, send_auto_auth_telegram_alert
from app.services.upstox_auto_auth import UpstoxAutoAuthError, perform_auto_login
from app.services.upstox_instruments import InstrumentRefreshError, refresh_upstox_instruments
from app.services.upstox_oauth import UpstoxOAuthError, exchange_authorization_code, store_access_token

router = APIRouter(prefix="/market-data", tags=["Market data"])
UPSTOX_AUTHORIZE_URL = "https://api-v2.upstox.com/login/authorization/dialog"


class OAuthStartResponse(BaseModel):
    authorization_url: str


class OAuthCallbackResponse(BaseModel):
    status: str
    expires_at: datetime


class InstrumentRefreshResponse(BaseModel):
    status: str
    instrument_count: int
    missing_keys: list[str]
    fetched_at: datetime


class MarketCandleResponse(BaseModel):
    opened_at: datetime
    closed_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


async def _redis(settings: AppSettings) -> Redis:
    return Redis.from_url(str(settings.redis_url), decode_responses=True)


@router.get("/brokers", response_model=BrokerControls)
async def broker_controls(settings: AppSettings, _: CurrentUser) -> BrokerControls:
    redis = await _redis(settings)
    try:
        return await load_broker_controls(redis)
    finally:
        await redis.aclose()


@router.put("/brokers", response_model=BrokerControls)
async def update_broker_controls(
    controls: BrokerControls,
    settings: AppSettings,
    user: User = Depends(require_roles(UserRole.ADMIN)),
) -> BrokerControls:
    redis = await _redis(settings)
    try:
        await save_broker_controls(redis, controls, user.id)
    finally:
        await redis.aclose()
    async with SessionLocal() as session:
        session.add(
            AuditLog(
                user_id=user.id,
                event_type="market_data.broker_controls_updated",
                metadata_json={"active_broker": controls.active_broker},
            )
        )
        await session.commit()
    return controls


class MarketRegimeResponse(BaseModel):
    enabled: bool
    available: bool
    regime: str | None = None
    score: float | None = None
    allow_long: bool | None = None
    allow_short: bool | None = None
    size_multiplier: float | None = None
    reason: str | None = None
    components: dict = {}
    vix: dict | None = None
    breadth: dict | None = None
    session_date: str | None = None
    computed_at: str | None = None


@router.get("/regime", response_model=MarketRegimeResponse)
async def market_regime(settings: AppSettings, _: CurrentUser) -> MarketRegimeResponse:
    redis = await _redis(settings)
    try:
        raw = await redis.get("market:regime")
    finally:
        await redis.aclose()
    if not raw:
        return MarketRegimeResponse(enabled=settings.market_regime_enabled, available=False)
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return MarketRegimeResponse(enabled=settings.market_regime_enabled, available=False)
    return MarketRegimeResponse(
        enabled=settings.market_regime_enabled,
        available=True,
        regime=data.get("regime"),
        score=data.get("score"),
        allow_long=data.get("allow_long"),
        allow_short=data.get("allow_short"),
        size_multiplier=data.get("size_multiplier"),
        reason=data.get("reason"),
        components=data.get("components", {}),
        vix=data.get("vix"),
        breadth=data.get("breadth"),
        session_date=data.get("session_date"),
        computed_at=data.get("computed_at"),
    )


@router.get("/candles/{instrument_token}", response_model=list[MarketCandleResponse])
async def market_candles(
    instrument_token: str,
    session: DbSession,
    _: CurrentUser,
    session_date: date | None = None,
    limit: int = Query(default=120, ge=10, le=390),
) -> list[MarketCandleResponse]:
    statement = select(MarketCandle).where(MarketCandle.instrument_token == instrument_token)
    if session_date:
        statement = statement.where(MarketCandle.session_date == session_date)
    rows = list((await session.scalars(statement.order_by(MarketCandle.opened_at.desc()).limit(limit))).all())
    return [
        MarketCandleResponse(
            opened_at=row.opened_at,
            closed_at=row.closed_at,
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=row.volume,
        )
        for row in reversed(rows)
    ]


@router.post("/upstox/authorize", response_model=OAuthStartResponse)
async def start_upstox_oauth(
    settings: AppSettings, user: User = Depends(require_roles(UserRole.ADMIN))
) -> OAuthStartResponse:
    if not settings.upstox_oauth_is_configured:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Upstox OAuth configuration is incomplete")
    state = secrets.token_urlsafe(32)
    redis = await _redis(settings)
    try:
        await redis.set(
            f"upstox:oauth:state:{state}",
            json.dumps({"user_id": str(user.id), "created_at": datetime.now(UTC).isoformat()}),
            ex=600,
            nx=True,
        )
    finally:
        await redis.aclose()
    query = urlencode(
        {
            "client_id": settings.upstox_api_key,
            "redirect_uri": str(settings.upstox_redirect_uri),
            "response_type": "code",
            "state": state,
        }
    )
    return OAuthStartResponse(authorization_url=f"{UPSTOX_AUTHORIZE_URL}?{query}")


@router.get("/upstox/callback", response_model=OAuthCallbackResponse)
async def complete_upstox_oauth(
    settings: AppSettings,
    code: str = Query(min_length=1, max_length=2048),
    state: str = Query(min_length=20, max_length=256),
) -> OAuthCallbackResponse:
    redis = await _redis(settings)
    try:
        raw = await redis.getdel(f"upstox:oauth:state:{state}")
    finally:
        await redis.aclose()
    try:
        expected = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        expected = {}
    user_id_raw = expected.get("user_id")
    if not user_id_raw:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired OAuth state")
    try:
        user_uuid = UUID(str(user_id_raw))
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid user identifier in OAuth state"
        ) from None

    async with SessionLocal() as session:
        user = await session.get(User, user_uuid)
        if user is None or not user.is_active or user.role != UserRole.ADMIN:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Unauthorized or inactive admin user")

    try:
        token = await exchange_authorization_code(settings, code)
        expires_at = await store_access_token(settings, token, user_uuid)
    except UpstoxOAuthError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    async with SessionLocal() as session:
        session.add(
            AuditLog(
                user_id=user_uuid,
                event_type="market_data.upstox_oauth_renewed",
                metadata_json={"expires_at": expires_at.isoformat()},
            )
        )
        await session.commit()
    return OAuthCallbackResponse(status="configured", expires_at=expires_at)


@router.post("/upstox/instruments/refresh", response_model=InstrumentRefreshResponse)
async def refresh_upstox_instrument_master(
    settings: AppSettings, user: User = Depends(require_roles(UserRole.ADMIN))
) -> InstrumentRefreshResponse:
    try:
        result = await refresh_upstox_instruments(settings)
    except InstrumentRefreshError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    async with SessionLocal() as session:
        session.add(
            AuditLog(
                user_id=user.id,
                event_type="market_data.upstox_instruments_refreshed",
                metadata_json={"missing_keys": result.missing_keys, "instrument_count": result.instrument_count},
            )
        )
        await session.commit()
    return InstrumentRefreshResponse(
        status="ok",
        instrument_count=result.instrument_count,
        missing_keys=result.missing_keys,
        fetched_at=result.fetched_at,
    )


# ── Auto-Auth endpoints ─────────────────────────────────────────────────


class AutoAuthStatus(BaseModel):
    enabled: bool
    configured: bool
    last_run_at: datetime | None = None
    last_success: bool | None = None
    expires_at: datetime | None = None
    error: str | None = None
    next_run: str | None = None


class AutoAuthTriggerResponse(BaseModel):
    status: str
    expires_at: datetime | None = None
    error: str | None = None


@router.get("/upstox/auto-auth/status", response_model=AutoAuthStatus)
async def upstox_auto_auth_status(settings: AppSettings, _: CurrentUser) -> AutoAuthStatus:
    """Return the current state of the Upstox auto-auth scheduler."""
    result = AutoAuthStatus(
        enabled=settings.upstox_auto_auth_enabled,
        configured=settings.upstox_auto_auth_is_configured,
    )

    # Read last result from Redis
    redis = await _redis(settings)
    try:
        raw = await redis.get(AUTO_AUTH_STATUS_KEY)
    finally:
        await redis.aclose()

    if raw:
        import json as _json

        try:
            data = _json.loads(raw)
            result.last_run_at = data.get("last_run_at")
            result.last_success = data.get("success")
            result.expires_at = data.get("expires_at")
            result.error = data.get("error")
        except (ValueError, TypeError):
            pass

    # The live scheduler instance is not reachable from here, so report the static schedule.
    if result.configured:
        result.next_run = "08:30 IST, next business day (Mon–Fri, excluding NSE holidays)"

    return result


@router.post("/upstox/auto-auth/trigger", response_model=AutoAuthTriggerResponse)
async def trigger_upstox_auto_auth(
    settings: AppSettings,
    user: User = Depends(require_roles(UserRole.ADMIN)),
) -> AutoAuthTriggerResponse:
    """Manually trigger the headless Upstox auto-login (admin only)."""
    if not settings.upstox_auto_auth_is_configured:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Upstox auto-auth is not configured. Set UPSTOX_MOBILE_NUMBER, UPSTOX_PIN, UPSTOX_TOTP_SECRET and UPSTOX_AUTO_AUTH_ENABLED in .env",
        )

    try:
        result = await perform_auto_login(settings)
    except UpstoxAutoAuthError as exc:
        # Log the manual trigger attempt
        async with SessionLocal() as session:
            session.add(
                AuditLog(
                    user_id=user.id,
                    event_type="market_data.upstox_auto_auth_manual_failed",
                    metadata_json={"error": str(exc)},
                )
            )
            await session.commit()
        await send_auto_auth_telegram_alert(
            settings=settings,
            trigger="Manual (Settings / API)",
            success=False,
            error=str(exc),
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    async with SessionLocal() as session:
        session.add(
            AuditLog(
                user_id=user.id,
                event_type="market_data.upstox_auto_auth_manual_success",
                metadata_json={"expires_at": result["expires_at"]},
            )
        )
        await session.commit()

    await send_auto_auth_telegram_alert(
        settings=settings,
        trigger="Manual (Settings / API)",
        success=True,
        expires_at=result["expires_at"],
    )

    # Update Redis status
    redis = await _redis(settings)
    try:
        import json as _json

        await redis.set(
            AUTO_AUTH_STATUS_KEY,
            _json.dumps(
                {
                    "last_run_at": result["renewed_at"],
                    "success": True,
                    "expires_at": result["expires_at"],
                    "error": None,
                    "trigger": "manual",
                }
            ),
            ex=86400,
        )
    finally:
        await redis.aclose()

    return AutoAuthTriggerResponse(
        status="ok",
        expires_at=result["expires_at"],
    )
