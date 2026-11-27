"""Research jobs: what is queued, running and finished, and how to stop one.

Read by anyone signed in; cancelling needs an administrator. These routes run
nothing themselves -- the research worker does.
"""

from datetime import UTC, date, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import func, select

from app.api.deps import AppSettings, CurrentUser, DbSession, require_roles
from app.db.models import (
    ResearchDataset,
    ResearchDatasetCoverage,
    ResearchExperiment,
    ResearchHypothesis,
    ResearchJob,
    ResearchJobEvent,
    User,
    UserRole,
)
from app.services import research_experiments, research_jobs
from app.services.research_worker_status import WORKER_HEARTBEAT_KEY, parse_heartbeat
from app.services.trading_calendar import TradingCalendar

router = APIRouter(prefix="/research", tags=["Research"])


class ResearchJobResponse(BaseModel):
    id: UUID
    job_type: str
    status: str
    attempts: int
    max_attempts: int
    cancel_requested: bool
    result: dict | None
    failure_detail: str | None
    worker_id: str | None
    created_at: datetime
    started_at: datetime | None
    heartbeat_at: datetime | None
    finished_at: datetime | None


class ResearchJobEventResponse(BaseModel):
    from_status: str | None
    to_status: str
    actor: str
    detail: str | None
    occurred_at: datetime


class ResearchJobDetail(ResearchJobResponse):
    events: list[ResearchJobEventResponse]


class ResearchWorkerStatus(BaseModel):
    status: str
    detail: str
    last_seen: datetime | None
    research_window_open: bool
    window_detail: str
    queued: int
    running: int


def _job(row: ResearchJob) -> ResearchJobResponse:
    return ResearchJobResponse(
        id=row.id,
        job_type=row.job_type,
        status=row.status,
        attempts=row.attempts,
        max_attempts=row.max_attempts,
        cancel_requested=row.cancel_requested,
        result=row.result,
        failure_detail=row.failure_detail,
        worker_id=row.worker_id,
        created_at=row.created_at,
        started_at=row.started_at,
        heartbeat_at=row.heartbeat_at,
        finished_at=row.finished_at,
    )


@router.get("/jobs", response_model=list[ResearchJobResponse])
async def list_jobs(
    _: CurrentUser, session: DbSession, limit: int = Query(default=30, ge=1, le=200)
) -> list[ResearchJobResponse]:
    rows = (await session.scalars(select(ResearchJob).order_by(ResearchJob.created_at.desc()).limit(limit))).all()
    return [_job(row) for row in rows]


@router.get("/jobs/{job_id}", response_model=ResearchJobDetail)
async def get_job(job_id: UUID, _: CurrentUser, session: DbSession) -> ResearchJobDetail:
    row = await session.get(ResearchJob, job_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Research job not found")
    events = (
        await session.scalars(
            select(ResearchJobEvent).where(ResearchJobEvent.job_id == job_id).order_by(ResearchJobEvent.occurred_at)
        )
    ).all()
    return ResearchJobDetail(
        **_job(row).model_dump(),
        events=[
            ResearchJobEventResponse(
                from_status=item.from_status,
                to_status=item.to_status,
                actor=item.actor,
                detail=item.detail,
                occurred_at=item.occurred_at,
            )
            for item in events
        ],
    )


@router.post("/jobs/{job_id}/cancel", response_model=ResearchJobResponse)
async def cancel_job(
    job_id: UUID, session: DbSession, user: User = Depends(require_roles(UserRole.ADMIN))
) -> ResearchJobResponse:
    try:
        row = await research_jobs.request_cancel(session, job_id, actor=f"user:{user.id}")
    except research_jobs.JobNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Research job not found") from exc
    await session.commit()
    await session.refresh(row)
    return _job(row)


@router.get("/worker", response_model=ResearchWorkerStatus)
async def worker_status(_: CurrentUser, session: DbSession, settings: AppSettings) -> ResearchWorkerStatus:
    now = datetime.now(UTC)
    gate = research_jobs.research_allowed(now, TradingCalendar.from_settings(settings))
    redis = Redis.from_url(str(settings.redis_url), decode_responses=True)
    try:
        raw = await redis.get(WORKER_HEARTBEAT_KEY)
    except Exception:  # noqa: BLE001 - a status read must not fail the page
        raw = None
    finally:
        await redis.aclose()
    seen = parse_heartbeat(raw, now)
    counts = {
        name: len((await session.scalars(select(ResearchJob.id).where(ResearchJob.status == name))).all())
        for name in (research_jobs.QUEUED, research_jobs.RUNNING)
    }
    return ResearchWorkerStatus(
        status=seen.status,
        detail=seen.detail,
        last_seen=seen.last_seen,
        research_window_open=gate.open,
        window_detail=gate.reason,
        queued=counts[research_jobs.QUEUED],
        running=counts[research_jobs.RUNNING],
    )


# --- datasets ---------------------------------------------------------------------------


class DatasetResponse(BaseModel):
    id: UUID
    name: str
    status: str
    timeframe: str
    source: str
    price_adjustment: str
    start_date: date
    end_date: date
    instrument_count: int
    row_count: int
    content_sha256: str | None
    sessions: int
    complete_instrument_sessions: int
    instrument_sessions: int
    created_at: datetime
    finalized_at: datetime | None


@router.get("/datasets", response_model=list[DatasetResponse])
async def list_datasets(_: CurrentUser, session: DbSession) -> list[DatasetResponse]:
    rows = (await session.scalars(select(ResearchDataset).order_by(ResearchDataset.created_at.desc()))).all()
    result = []
    for row in rows:
        coverage = (
            await session.execute(
                select(
                    func.count(func.distinct(ResearchDatasetCoverage.session_date)),
                    func.count().filter(ResearchDatasetCoverage.missing_bars == 0),
                    func.count(),
                ).where(ResearchDatasetCoverage.dataset_id == row.id, ResearchDatasetCoverage.expected_bars > 0)
            )
        ).one()
        result.append(
            DatasetResponse(
                id=row.id,
                name=row.name,
                status=row.status,
                timeframe=row.timeframe,
                source=row.source,
                price_adjustment=row.price_adjustment,
                start_date=row.start_date,
                end_date=row.end_date,
                instrument_count=row.instrument_count,
                row_count=row.row_count,
                content_sha256=row.content_sha256,
                sessions=coverage[0] or 0,
                complete_instrument_sessions=coverage[1] or 0,
                instrument_sessions=coverage[2] or 0,
                created_at=row.created_at,
                finalized_at=row.finalized_at,
            )
        )
    return result


# --- hypotheses and experiments ----------------------------------------------------------


class HypothesisRequest(BaseModel):
    title: str = Field(min_length=3, max_length=120)
    statement: str = Field(min_length=10, max_length=4000)
    rationale: str | None = Field(default=None, max_length=4000)


class HypothesisResponse(BaseModel):
    id: UUID
    title: str
    statement: str
    rationale: str | None
    status: str
    created_at: datetime


@router.get("/hypotheses", response_model=list[HypothesisResponse])
async def list_hypotheses(_: CurrentUser, session: DbSession) -> list[HypothesisResponse]:
    rows = (await session.scalars(select(ResearchHypothesis).order_by(ResearchHypothesis.created_at.desc()))).all()
    return [HypothesisResponse.model_validate(row, from_attributes=True) for row in rows]


@router.post("/hypotheses", response_model=HypothesisResponse, status_code=status.HTTP_201_CREATED)
async def create_hypothesis(
    request: HypothesisRequest, session: DbSession, user: User = Depends(require_roles(UserRole.ADMIN))
) -> HypothesisResponse:
    row = await research_experiments.create_hypothesis(
        session, title=request.title, statement=request.statement, rationale=request.rationale, user_id=user.id
    )
    await session.commit()
    await session.refresh(row)
    return HypothesisResponse.model_validate(row, from_attributes=True)


class ExperimentRequest(BaseModel):
    hypothesis_id: UUID
    name: str = Field(min_length=3, max_length=120)
    dataset_id: UUID
    strategy_id: str
    instruments: list[str] = Field(min_length=1, max_length=250)
    parameter_grid: dict[str, list] = Field(min_length=1)
    selection_start: date
    selection_end: date
    holdout_start: date
    holdout_end: date
    train_sessions: int = Field(default=60, ge=5, le=500)
    test_sessions: int = Field(default=20, ge=1, le=250)
    success_mean_r: float = Field(default=0.1, ge=-1, le=5)


class ExperimentResponse(BaseModel):
    id: UUID
    hypothesis_id: UUID
    name: str
    dataset_id: UUID
    instruments: list
    strategy: dict
    parameter_grid: dict
    variants: int
    selection_start: date
    selection_end: date
    holdout_start: date
    holdout_end: date
    train_sessions: int
    test_sessions: int
    success_mean_r: float
    status: str
    chosen_index: int | None
    chosen_parameters: dict | None
    holdout_used: bool
    verdict: str | None
    registered_at: datetime | None
    created_at: datetime


class ExperimentResultResponse(BaseModel):
    kind: str
    code_fingerprint: str
    dataset_sha256: str
    summary: dict
    created_at: datetime


class ExperimentDetail(ExperimentResponse):
    results: list[ExperimentResultResponse]


def _experiment(row: ResearchExperiment) -> ExperimentResponse:
    values = {column.name: getattr(row, column.name) for column in ResearchExperiment.__table__.columns}
    values["success_mean_r"] = float(row.success_mean_r)
    return ExperimentResponse.model_validate(values)


@router.get("/experiments", response_model=list[ExperimentResponse])
async def list_experiments(_: CurrentUser, session: DbSession) -> list[ExperimentResponse]:
    rows = (await session.scalars(select(ResearchExperiment).order_by(ResearchExperiment.created_at.desc()))).all()
    return [_experiment(row) for row in rows]


@router.get("/experiments/{experiment_id}", response_model=ExperimentDetail)
async def get_experiment(experiment_id: UUID, _: CurrentUser, session: DbSession) -> ExperimentDetail:
    row = await session.get(ResearchExperiment, experiment_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Experiment not found")
    results = await research_experiments.results_of(session, experiment_id)
    return ExperimentDetail(
        **_experiment(row).model_dump(),
        results=[ExperimentResultResponse.model_validate(item, from_attributes=True) for item in results],
    )


@router.post("/experiments", response_model=ExperimentResponse, status_code=status.HTTP_201_CREATED)
async def create_experiment(
    request: ExperimentRequest, session: DbSession, user: User = Depends(require_roles(UserRole.ADMIN))
) -> ExperimentResponse:
    """A DRAFT experiment. The strategy, controls and charges are copied now, as they stand."""
    from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY, TradingControls
    from app.db.models import ApplicationSetting
    from app.services.paper_execution import DEFAULT_PAPER_EXECUTION_CONTROLS, PAPER_EXECUTION_KEY
    from app.services.strategy_registry import DEFAULT_STRATEGIES, STRATEGIES_KEY, StrategyConfiguration

    if await session.get(ResearchHypothesis, request.hypothesis_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hypothesis not found")
    stored = await session.get(ApplicationSetting, STRATEGIES_KEY)
    strategies = [
        StrategyConfiguration.model_validate(item) for item in (stored.value if stored else DEFAULT_STRATEGIES)
    ]
    strategy = next((item for item in strategies if item.id == request.strategy_id), None)
    if strategy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strategy not found")
    controls_row = await session.get(ApplicationSetting, TRADING_KEY)
    controls = TradingControls.model_validate(controls_row.value if controls_row else DEFAULT_TRADING_CONTROLS)
    execution_row = await session.get(ApplicationSetting, PAPER_EXECUTION_KEY)
    try:
        row = await research_experiments.create_experiment(
            session,
            hypothesis_id=request.hypothesis_id,
            name=request.name,
            dataset_id=request.dataset_id,
            instruments=list(dict.fromkeys(item.strip() for item in request.instruments if item.strip())),
            strategy=strategy.model_dump(mode="json"),
            controls=controls.model_dump(mode="json"),
            execution=execution_row.value if execution_row else DEFAULT_PAPER_EXECUTION_CONTROLS,
            parameter_grid=request.parameter_grid,
            selection_start=request.selection_start,
            selection_end=request.selection_end,
            holdout_start=request.holdout_start,
            holdout_end=request.holdout_end,
            train_sessions=request.train_sessions,
            test_sessions=request.test_sessions,
            success_mean_r=request.success_mean_r,
            created_by_user_id=user.id,
        )
    except research_experiments.ExperimentError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    await session.commit()
    await session.refresh(row)
    return _experiment(row)


@router.post("/experiments/{experiment_id}/register", response_model=ExperimentResponse)
async def register_experiment(
    experiment_id: UUID, session: DbSession, user: User = Depends(require_roles(UserRole.ADMIN))
) -> ExperimentResponse:
    """Freeze the experiment and queue its selection. Nothing that decides the answer can change after this."""
    try:
        row = await research_experiments.register(session, experiment_id, user_id=user.id)
    except research_experiments.ExperimentError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    await session.refresh(row)
    return _experiment(row)


class HoldoutRequest(BaseModel):
    # The holdout can be looked at once. The screen asks for this explicitly.
    confirm_single_use: bool = False


@router.post("/experiments/{experiment_id}/holdout", response_model=ExperimentResponse)
async def score_holdout(
    experiment_id: UUID,
    request: HoldoutRequest,
    session: DbSession,
    user: User = Depends(require_roles(UserRole.ADMIN)),
) -> ExperimentResponse:
    if not request.confirm_single_use:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The holdout is scored once and never again. Confirm to continue.",
        )
    try:
        row = await research_experiments.request_holdout(session, experiment_id, user_id=user.id)
    except research_experiments.ExperimentError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    await session.refresh(row)
    return _experiment(row)
