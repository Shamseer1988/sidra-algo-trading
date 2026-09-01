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
