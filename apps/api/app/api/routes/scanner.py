import json
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import select

from app.api.deps import AppSettings, CurrentUser, DbSession, require_roles
from app.db.models import AuditLog, PaperSignal, ScannerEvaluation, User, UserRole
from app.db.session import SessionLocal
from app.services.data_quality import DATA_QUALITY_PREFIX
from app.services.safety import emergency_stop_state
from app.services.worker_supervision import WORKER_STATE_KEY

router = APIRouter(prefix="/scanner", tags=["Scanner"])
SCANNER_CONTROL_KEY = "scanner:control_state"
SCANNER_HEARTBEAT_KEY = "scanner:heartbeat"


class ScannerStatus(BaseModel):
    status: str
    last_heartbeat: datetime | None = None
    detail: str
    worker_restart_count: int = 0


class DataQualityResponse(BaseModel):
    instrument_token: str
    state: str
    reason: str
    session_date: date
    expected_bars: int
    received_bars: int
    missing_buckets: list[str]
    received_ticks: int
    duplicate_ticks: int
    out_of_order_ticks: int
    invalid_ticks: int
    average_latency_ms: int
    max_latency_ms: int
    last_exchange_timestamp: datetime | None
    last_received_timestamp: datetime | None
    observed_at: datetime
    allows_signals: bool


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


class ScannerEvaluationResponse(BaseModel):
    id: str
    instrument_token: str
    session_date: str
    candle_opened_at: datetime
    strategy_id: str
    strategy_name: str
    strategy_version: int
    status: str
    decision_state: str
    side: str | None
    reason: str
    failed_conditions: list[str]
    data_quality_state: str
    candle_close: float
    candle_volume: int
    score: int
    score_breakdown: dict
    indicator_snapshot: dict
    entry_price: float | None
    stop_price: float | None
    target_price: float | None
    quantity: int | None
    risk_amount: float | None
    created_at: datetime


async def get_scanner_status(redis: Redis) -> ScannerStatus:
    control_state = await redis.get(SCANNER_CONTROL_KEY) or "STOPPED"
    heartbeat = await redis.get(SCANNER_HEARTBEAT_KEY)
    worker_state = await redis.hgetall(WORKER_STATE_KEY)
    restart_count = int(worker_state.get("restart_count", 0))
    parsed_heartbeat = datetime.fromisoformat(heartbeat) if heartbeat else None
    if control_state == "RUNNING" and parsed_heartbeat and (datetime.now(UTC) - parsed_heartbeat).total_seconds() > 90:
        return ScannerStatus(
            status="DEGRADED",
            last_heartbeat=parsed_heartbeat,
            detail="Worker heartbeat is stale",
            worker_restart_count=restart_count,
        )
    if control_state == "RUNNING" and worker_state.get("status") == "DEGRADED":
        return ScannerStatus(
            status="DEGRADED",
            last_heartbeat=parsed_heartbeat,
            detail=worker_state.get("detail", "Scanner worker is recovering"),
            worker_restart_count=restart_count,
        )
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
        worker_restart_count=restart_count,
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


def _evaluation_response(row: ScannerEvaluation) -> ScannerEvaluationResponse:
    return ScannerEvaluationResponse(
        id=str(row.id),
        instrument_token=row.instrument_token,
        session_date=row.session_date.isoformat(),
        candle_opened_at=row.candle_opened_at,
        strategy_id=row.strategy_id,
        strategy_name=row.strategy_name,
        strategy_version=row.strategy_version,
        status=row.status,
        decision_state=row.decision_state,
        side=row.side,
        reason=row.reason,
        failed_conditions=[str(item) for item in row.failed_conditions],
        data_quality_state=row.data_quality_state,
        candle_close=float(row.candle_close),
        candle_volume=row.candle_volume,
        score=row.score,
        score_breakdown=row.score_breakdown,
        indicator_snapshot=row.indicator_snapshot,
        entry_price=float(row.entry_price) if row.entry_price is not None else None,
        stop_price=float(row.stop_price) if row.stop_price is not None else None,
        target_price=float(row.target_price) if row.target_price is not None else None,
        quantity=row.quantity,
        risk_amount=float(row.risk_amount) if row.risk_amount is not None else None,
        created_at=row.created_at,
    )


@router.get("/evaluations", response_model=list[ScannerEvaluationResponse])
async def latest_scanner_evaluations(
    _: CurrentUser,
    session: DbSession,
    limit: int = 100,
    offset: int = 0,
) -> list[ScannerEvaluationResponse]:
    bounded_limit = min(max(limit, 1), 250)
    bounded_offset = max(offset, 0)
    rows = list(
        (
            await session.scalars(
                select(ScannerEvaluation)
                .order_by(ScannerEvaluation.candle_opened_at.desc(), ScannerEvaluation.created_at.desc())
                .offset(bounded_offset)
                .limit(bounded_limit)
            )
        ).all()
    )
    return [_evaluation_response(row) for row in rows]


@router.get("/evaluations/{evaluation_id}", response_model=ScannerEvaluationResponse)
async def scanner_evaluation_detail(
    evaluation_id: str, _: CurrentUser, session: DbSession
) -> ScannerEvaluationResponse:
    try:
        row = await session.get(ScannerEvaluation, evaluation_id)
    except (TypeError, ValueError):
        row = None
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scanner evaluation was not found")
    return _evaluation_response(row)


@router.get("/data-quality", response_model=list[DataQualityResponse])
async def current_data_quality(settings: AppSettings, _: CurrentUser) -> list[DataQualityResponse]:
    redis = await _redis(settings)
    snapshots: list[DataQualityResponse] = []
    try:
        async for key in redis.scan_iter(match=f"{DATA_QUALITY_PREFIX}*", count=100):
            raw = await redis.get(key)
            if not raw:
                continue
            try:
                snapshots.append(DataQualityResponse.model_validate(json.loads(raw)))
            except (json.JSONDecodeError, ValueError):
                continue
    finally:
        await redis.aclose()
    return sorted(snapshots, key=lambda item: item.instrument_token)


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
