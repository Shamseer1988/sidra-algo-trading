import json
from datetime import UTC, date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import select

from app.api.deps import AppSettings, CurrentUser, DbSession, require_roles
from app.db.models import AuditLog, PaperSignal, ScannerEvaluation, User, UserRole
from app.db.session import SessionLocal
from app.services import scanner_report
from app.services.data_quality import DATA_QUALITY_PREFIX
from app.services.safety import emergency_stop_state
from app.services.trading_symbols import resolve_script_names, resolve_symbol
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
    script_name: str = ""
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
    script_name: str = ""
    session_date: str
    candle_opened_at: datetime
    side: str
    strategy_version: str = ""
    status: str
    entry_price: float
    stop_price: float
    target_price: float
    quantity: int
    score: int
    score_breakdown: dict
    strategy_snapshot: dict
    created_at: datetime


class ScannerEvaluationResponse(BaseModel):
    id: str
    instrument_token: str
    script_name: str = ""
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
    strategy_snapshot: dict
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
        "RUNNING": "Worker is active. Completed-candle calculations run each minute once a market-data connector is streaming.",
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
    names = await resolve_script_names(session, {row.instrument_token for row in rows})
    return [
        PaperSignalResponse(
            id=str(row.id),
            instrument_token=row.instrument_token,
            script_name=names.get(row.instrument_token, row.instrument_token),
            session_date=row.session_date.isoformat(),
            candle_opened_at=row.candle_opened_at,
            side=row.side,
            strategy_version=row.strategy_version,
            status=row.status,
            entry_price=float(row.entry_price),
            stop_price=float(row.stop_price),
            target_price=float(row.target_price),
            quantity=row.quantity,
            score=row.score,
            score_breakdown=row.score_breakdown,
            strategy_snapshot=row.strategy_snapshot,
            created_at=row.created_at,
        )
        for row in rows
    ]


def _evaluation_response(row: ScannerEvaluation, script_name: str = "") -> ScannerEvaluationResponse:
    return ScannerEvaluationResponse(
        id=str(row.id),
        instrument_token=row.instrument_token,
        script_name=script_name or row.instrument_token,
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
        strategy_snapshot=row.strategy_snapshot,
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
    names = await resolve_script_names(session, {row.instrument_token for row in rows})
    return [_evaluation_response(row, names.get(row.instrument_token, "")) for row in rows]


class Moment(BaseModel):
    """One check worth reading: what was seen, when, and what was decided."""

    id: str
    instrument_token: str
    script_name: str
    at: datetime
    strategy_name: str
    side: str | None
    score: int
    reason: str


class DayTotals(BaseModel):
    checks: int
    outside_hours: int
    blocked_by_data: int
    scored: int
    accepted: int
    shares: int
    last_candle: datetime | None


class StrategyDay(BaseModel):
    strategy_id: str
    strategy_name: str
    checks: int
    outside_hours: int
    scored: int
    accepted: int
    best_score: int


class ReasonCount(BaseModel):
    reason: str
    count: int


class ShareDay(BaseModel):
    instrument_token: str
    script_name: str
    last_close: float | None
    accepted: int
    scored: int
    blocked: int
    best: Moment | None


class ScannerDay(BaseModel):
    session_date: date
    sessions: list[date]
    totals: DayTotals
    strategies: list[StrategyDay]
    reasons: list[ReasonCount]
    signals: list[Moment]
    near: list[Moment]
    shares: list[ShareDay]


@router.get("/day", response_model=ScannerDay)
async def scanner_day(_: CurrentUser, session: DbSession, session_date: date | None = None) -> ScannerDay:
    """One session summed up. Without a date, the newest session that was scanned: on a holiday, the last one."""
    sessions = await scanner_report.recent_sessions(session)
    day = session_date or (sessions[0] if sessions else datetime.now(UTC).date())
    return ScannerDay(sessions=sessions, **await scanner_report.cached(session, day))


class EvaluationPage(BaseModel):
    total: int
    items: list[ScannerEvaluationResponse]


@router.get("/evaluations/page", response_model=EvaluationPage)
async def evaluation_page(
    _: CurrentUser,
    session: DbSession,
    session_date: date,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
    status: Literal["ACCEPTED", "WATCHING", "REJECTED"] | None = None,
    strategy_id: str | None = None,
    quality: str | None = None,
    search: Annotated[str | None, Query(max_length=60)] = None,
    scored_only: bool = False,
    hide_outside: bool = False,
    sort: Literal["time", "score"] = "time",
) -> EvaluationPage:
    total, rows = await scanner_report.page(
        session,
        scanner_report.Filters(
            day=session_date,
            status=status,
            strategy_id=strategy_id,
            quality=quality,
            search=search,
            scored_only=scored_only,
            hide_outside=hide_outside,
            by_score=sort == "score",
        ),
        limit,
        offset,
    )
    names = await resolve_script_names(session, {row.instrument_token for row in rows})
    return EvaluationPage(
        total=total, items=[_evaluation_response(row, names.get(row.instrument_token, "")) for row in rows]
    )


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
    return _evaluation_response(row, await resolve_symbol(session, row.instrument_token))


@router.get("/data-quality", response_model=list[DataQualityResponse])
async def current_data_quality(
    settings: AppSettings, _: CurrentUser, session: DbSession
) -> list[DataQualityResponse]:
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
    names = await resolve_script_names(session, {item.instrument_token for item in snapshots})
    for item in snapshots:
        item.script_name = names.get(item.instrument_token, item.instrument_token)
    return sorted(snapshots, key=lambda item: item.script_name)


async def _set_state(state: str, user: User, settings: AppSettings) -> ScannerStatus:
    redis = await _redis(settings)
    try:
        await redis.set(SCANNER_CONTROL_KEY, state)
        if state == "RUNNING":
            # Two switches reached the same outcome by different routes: this
            # one stops the worker's data loop, the paper-tracking flag stops
            # signal evaluation. Nobody needs both, and leaving the second
            # behind meant Start could appear to do nothing -- the worker runs,
            # no signals appear, and the reason is on another screen.
            #
            # Clearing it on start, and not touching it on stop, keeps Start
            # honest without inventing a third state. The flag still exists for
            # the scanner to read; it is simply no longer a separate button an
            # operator has to know about.
            from app.services.safety import PAPER_TRACKING_KEY

            await redis.set(PAPER_TRACKING_KEY, "true")
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
