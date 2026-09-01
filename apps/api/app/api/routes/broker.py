from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from redis.asyncio import Redis

from app.api.deps import AppSettings, CurrentUser, require_roles
from app.db.models import AuditLog, User, UserRole
from app.db.session import SessionLocal
from app.services.firstock.client import FirstockClient, FirstockError
from app.services.firstock.market_data import CONNECTION_STATE_KEY, configured_subscriptions

router = APIRouter(prefix="/broker/firstock", tags=["Broker"])


class FirstockBrokerStatus(BaseModel):
    configured: bool
    user_id_masked: str | None
    websocket_status: str
    detail: str
    updated_at: datetime | None
    subscription_count: int


def _masked_user_id(user_id: str | None) -> str | None:
    if not user_id:
        return None
    return f"{'*' * max(0, len(user_id) - 3)}{user_id[-3:]}"


async def _status(settings: AppSettings) -> FirstockBrokerStatus:
    redis = Redis.from_url(str(settings.redis_url), decode_responses=True)
    try:
        state = await redis.hgetall(CONNECTION_STATE_KEY)
    finally:
        await redis.aclose()
    updated_at = datetime.fromisoformat(state["updated_at"]) if state.get("updated_at") else None
    return FirstockBrokerStatus(
        configured=settings.firstock_is_configured,
        user_id_masked=_masked_user_id(settings.firstock_user_id),
        websocket_status=state.get(
            "status", "NOT_CONFIGURED" if not settings.firstock_is_configured else "DISCONNECTED"
        ),
        detail=state.get(
            "detail",
            "Credentials are configured in the server environment only"
            if settings.firstock_is_configured
            else "Set server-side Firstock credentials to connect",
        ),
        updated_at=updated_at,
        subscription_count=len(configured_subscriptions(settings)),
    )


@router.get("/status", response_model=FirstockBrokerStatus)
async def broker_status(settings: AppSettings, _: CurrentUser) -> FirstockBrokerStatus:
    return await _status(settings)


@router.post("/test", response_model=FirstockBrokerStatus)
async def test_connection(
    settings: AppSettings, user: User = Depends(require_roles(UserRole.ADMIN))
) -> FirstockBrokerStatus:
    """Authenticate only; this endpoint neither opens an order channel nor submits orders."""
    try:
        await FirstockClient(settings).login()
        detail = "Firstock REST authentication succeeded"
        state = "AUTHENTICATED"
    except FirstockError:
        detail = "Firstock REST authentication failed; check server-side credentials and IP allowlist"
        state = "ERROR"
    redis = Redis.from_url(str(settings.redis_url), decode_responses=True)
    try:
        await redis.hset(
            CONNECTION_STATE_KEY,
            mapping={"status": state, "detail": detail, "updated_at": datetime.now(UTC).isoformat()},
        )
    finally:
        await redis.aclose()
    async with SessionLocal() as session:
        session.add(
            AuditLog(user_id=user.id, event_type="broker.firstock_connection_test", metadata_json={"result": state})
        )
        await session.commit()
    return await _status(settings)
