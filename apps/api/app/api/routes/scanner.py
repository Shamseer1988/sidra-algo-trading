from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import select

from app.api.deps import AppSettings, CurrentUser, DbSession, require_roles
from app.db.models import AuditLog, PaperSignal, User, UserRole
from app.db.session import SessionLocal
from app.services.safety import emergency_stop_state

router = APIRouter(prefix="/scanner", tags=["Scanner"])
SCANNER_CONTROL_KEY = "scanner:control_state"
SCANNER_HEARTBEAT_KEY = "scanner:heartbeat"


class ScannerStatus(BaseModel):
    status: str
    last_heartbeat: datetime | None = None
    detail: str


class PaperSignalResponse(BaseModel):
    id: str
    instrument_token: str
    session_date: str
    candle_opened_at: datetime
    side: str
    status: str
    entry_price: float
    stop_price: float
    target_price: float
    quantity: int
    score: int
    score_breakdown: dict
    created_at: datetime


async def get_scanner_status(redis: Redis) -> ScannerStatus:
    control_state = await redis.get(SCANNER_CONTROL_KEY) or "STOPPED"
    heartbeat = await redis.get(SCANNER_HEARTBEAT_KEY)
    parsed_heartbeat = datetime.fromisoformat(heartbeat) if heartbeat else None
    if control_state == "RUNNING" and parsed_heartbeat and (datetime.now(UTC) - parsed_heartbeat).total_seconds() > 90:
        return ScannerStatus(status="DEGRADED", last_heartbeat=parsed_heartbeat, detail="Worker heartbeat is stale")
    descriptions = {
        "STOPPED": "Scanner is paused; no market data or signals are processed.",
        "STARTING": "Scanner startup has been requested.",
        "RUNNING": "Worker is active; completed-candle calculations begin when Firstock market data is configured.",
        "DEGRADED": "Scanner needs attention.",
    }
    return ScannerStatus(
        status=control_state,
        last_heartbeat=parsed_heartbeat,
        detail=descriptions.get(control_state, "Unknown scanner state"),
    )


async def _redis(settings: AppSettings) -> Redis:
    return Redis.from_url(str(settings.redis_url), decode_responses=True)


@router.get("/status", response_model=ScannerStatus)
async def get_current_status(settings: AppSettings, _: CurrentUser) -> ScannerStatus:
    redis = await _redis(settings)
    try:
        return await get_scanner_status(redis)
    finally:
        await redis.aclose()


@router.get("/signals", response_model=list[PaperSignalResponse])
async def latest_paper_signals(_: CurrentUser, session: DbSession) -> list[PaperSignalResponse]:
    rows = list((await session.scalars(select(PaperSignal).order_by(PaperSignal.created_at.desc()).limit(100))).all())
    return [
        PaperSignalResponse(
            id=str(row.id),
            instrument_token=row.instrument_token,
            session_date=row.session_date.isoformat(),
            candle_opened_at=row.candle_opened_at,
            side=row.side,
            status=row.status,
            entry_price=float(row.entry_price),
            stop_price=float(row.stop_price),
            target_price=float(row.target_price),
            quantity=row.quantity,
            score=row.score,
            score_breakdown=row.score_breakdown,
            created_at=row.created_at,
        )
        for row in rows
    ]


async def _set_state(state: str, user: User, settings: AppSettings) -> ScannerStatus:
    redis = await _redis(settings)
    try:
        await redis.set(SCANNER_CONTROL_KEY, state)
        result = await get_scanner_status(redis)
    finally:
        await redis.aclose()
    async with SessionLocal() as session:
        session.add(
            AuditLog(
                user_id=user.id,
                event_type=f"scanner.{state.lower()}",
                metadata_json={"requested_at": datetime.now(UTC).isoformat()},
            )
        )
        await session.commit()
    return result


@router.post("/start", response_model=ScannerStatus)
async def start(
    settings: AppSettings, user: User = Depends(require_roles(UserRole.ADMIN, UserRole.TRADER))
) -> ScannerStatus:
    redis = await _redis(settings)
    try:
        if (await emergency_stop_state(redis)).get("active") == "true":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Emergency stop is active; clear it before starting the scanner",
            )
    finally:
        await redis.aclose()
    return await _set_state("RUNNING", user, settings)


@router.post("/stop", response_model=ScannerStatus)
async def stop(
    settings: AppSettings, user: User = Depends(require_roles(UserRole.ADMIN, UserRole.TRADER))
) -> ScannerStatus:
    return await _set_state("STOPPED", user, settings)
