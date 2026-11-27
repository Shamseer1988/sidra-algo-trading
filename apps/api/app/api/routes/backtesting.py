"""Completed-candle-only historical research APIs; these routes cannot submit orders.

Since Research Lab Phase 3 these routes run no replay themselves. A backtest or
sweep becomes a ``research_jobs`` row; the ``research-worker`` container runs it
(``app.research_worker``) and the route waits for the answer, so the API
process -- which also runs the live exit sweep and reconciliation -- never
spends its CPU on research.
"""

from datetime import UTC, date, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select

from app.api.deps import AppSettings, CurrentUser, DbSession, require_roles
from app.api.routes.settings import (
    DEFAULT_TRADING_CONTROLS,
    TRADING_KEY,
    TradingControls,
    _persist_strategies,
    _stored_strategies,
    strategy_mode,
)
from app.db.models import (
    ApplicationSetting,
    BacktestRun,
    BacktestSweep,
    BacktestTrade,
    ResearchJob,
    User,
    UserRole,
)
from app.services import research_jobs, research_runs
from app.services.backtest_sweep import STRATEGY_PARAMS, SWEEPABLE
from app.services.paper_execution import DEFAULT_PAPER_EXECUTION_CONTROLS, PAPER_EXECUTION_KEY, PaperExecutionControls
from app.services.research_runs import sector_lookup  # noqa: F401 - re-exported for scripts
from app.services.strategy_registry import DEFAULT_STRATEGIES, STRATEGIES_KEY, StrategyConfiguration, StrategyRegistry
from app.services.trading_calendar import TradingCalendar

# How long a request waits for the worker before answering "still running".
# A single backtest is minutes; a sweep can be an hour and is never waited for
# that long -- it appears in the sweep list when it is done.
BACKTEST_WAIT_SECONDS = 600
SWEEP_WAIT_SECONDS = 60

router = APIRouter(prefix="/backtests", tags=["Backtesting"])


class BacktestRunRequest(BaseModel):
    start_date: date
    end_date: date
    instrument_tokens: list[str] = Field(min_length=1, max_length=10)
    strategy_ids: list[str] = Field(default_factory=list, max_length=10)
    timeframe_seconds: int = Field(default=60, ge=60, le=900)
    # A finalised research dataset to measure on instead of the live candle store.
    dataset_id: UUID | None = None

    @field_validator("instrument_tokens", "strategy_ids")
    @classmethod
    def normalize_values(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(item.strip() for item in values if item.strip()))
        if not normalized and values:
            raise ValueError("At least one valid value is required")
        return normalized

    @model_validator(mode="after")
    def validate_range(self) -> "BacktestRunRequest":
        if self.end_date < self.start_date:
            raise ValueError("end_date must not precede start_date")
        if (self.end_date - self.start_date).days > 31:
            raise ValueError("Backtests are limited to 32 calendar days per run")
        return self


class BacktestRunResponse(BaseModel):
    id: str
    status: str
    start_date: date
    end_date: date
    timeframe_seconds: int
    instrument_tokens: list[str]
    source_candle_count: int
    data_fingerprint: str
    initial_capital: float
    final_equity: float | None
    net_pnl: float | None
    max_drawdown: float | None
    summary: dict
    failure_detail: str | None
    created_at: datetime


class BacktestTradeResponse(BaseModel):
    id: str
    strategy_id: str
    strategy_name: str
    strategy_version: int
    instrument_token: str
    session_date: date
    side: str
    quantity: int
    signal_at: datetime
    entered_at: datetime
    exited_at: datetime
    entry_price: float
    exit_price: float
    gross_pnl: float
    fees_total: float
    net_pnl: float
    realized_r: float
    exit_reason: str


class BacktestRunDetail(BacktestRunResponse):
    trades: list[BacktestTradeResponse]


def _run_response(row: BacktestRun) -> BacktestRunResponse:
    return BacktestRunResponse(
        id=str(row.id),
        status=row.status,
        start_date=row.start_date,
        end_date=row.end_date,
        timeframe_seconds=row.timeframe_seconds,
        instrument_tokens=row.instrument_tokens,
        source_candle_count=row.source_candle_count,
        data_fingerprint=row.data_fingerprint,
        initial_capital=float(row.initial_capital),
        final_equity=float(row.final_equity) if row.final_equity is not None else None,
        net_pnl=float(row.net_pnl) if row.net_pnl is not None else None,
        max_drawdown=float(row.max_drawdown) if row.max_drawdown is not None else None,
        summary=row.result_summary,
        failure_detail=row.failure_detail,
        created_at=row.created_at,
    )


def _trade_response(row: BacktestTrade) -> BacktestTradeResponse:
    return BacktestTradeResponse(
        id=str(row.id),
        strategy_id=row.strategy_id,
        strategy_name=row.strategy_name,
        strategy_version=row.strategy_version,
        instrument_token=row.instrument_token,
        session_date=row.session_date,
        side=row.side,
        quantity=row.quantity,
        signal_at=row.signal_at,
        entered_at=row.entered_at,
        exited_at=row.exited_at,
        entry_price=float(row.entry_price),
        exit_price=float(row.exit_price),
        gross_pnl=float(row.gross_pnl),
        fees_total=float(row.fees_total),
        net_pnl=float(row.net_pnl),
        realized_r=float(row.realized_r),
        exit_reason=row.exit_reason,
    )


async def _run_as_job(
    session: DbSession,
    app_settings,  # noqa: ANN001 - Settings
    job_type: str,
    payload: dict,
    user: User,
    *,
    wait_seconds: float,
) -> ResearchJob:
    """Queue the job for the research worker and wait for it, within reason.

    Committed before waiting, so the job exists even if this request is
    abandoned. Inside market hours the worker will not start it, so the route
    says so at once instead of holding the request open until 3:40 PM.
    """
    job = await research_jobs.enqueue(session, job_type, payload, user_id=user.id)
    await session.commit()
    gate = research_jobs.research_allowed(datetime.now(UTC), TradingCalendar.from_settings(app_settings))
    if not gate.open:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{gate.reason} Queued as research job {job.id}; the result appears in the list once it has run.",
        )
    finished = await research_jobs.wait_for(job.id, timeout_seconds=wait_seconds)
    if finished is None or finished.status not in research_jobs.FINISHED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Still running as research job {job.id}; the result appears in the list when it finishes.",
        )
    if finished.status != research_jobs.SUCCEEDED:
        code = (
            status.HTTP_409_CONFLICT
            if finished.status == research_jobs.CANCELLED
            else status.HTTP_422_UNPROCESSABLE_ENTITY
        )
        raise HTTPException(
            status_code=code, detail=finished.failure_detail or f"Research job {finished.status.lower()}"
        )
    return finished


@router.get("", response_model=list[BacktestRunResponse])
async def list_backtests(
    _: CurrentUser, session: DbSession, limit: int = Query(default=20, ge=1, le=100)
) -> list[BacktestRunResponse]:
    rows = list((await session.scalars(select(BacktestRun).order_by(BacktestRun.created_at.desc()).limit(limit))).all())
    return [_run_response(row) for row in rows]


@router.post("/run", response_model=BacktestRunDetail, status_code=status.HTTP_201_CREATED)
async def create_backtest(
    request: BacktestRunRequest,
    app_settings: AppSettings,
    session: DbSession,
    user: User = Depends(require_roles(UserRole.ADMIN)),
) -> BacktestRunDetail:
    setting = await session.get(ApplicationSetting, TRADING_KEY)
    controls = TradingControls.model_validate(setting.value if setting else DEFAULT_TRADING_CONTROLS).model_dump()
    execution_setting = await session.get(ApplicationSetting, PAPER_EXECUTION_KEY)
    execution_controls = PaperExecutionControls.model_validate(
        execution_setting.value if execution_setting else DEFAULT_PAPER_EXECUTION_CONTROLS
    )
    strategies_setting = await session.get(ApplicationSetting, STRATEGIES_KEY)
    candidates = [
        StrategyConfiguration.model_validate(item)
        for item in (strategies_setting.value if strategies_setting else DEFAULT_STRATEGIES)
    ]
    selected = [
        item for item in candidates if item.enabled and (not request.strategy_ids or item.id in request.strategy_ids)
    ]
    if request.strategy_ids and len(selected) != len(request.strategy_ids):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="One or more selected strategies are unavailable"
        )
    if not selected:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="No enabled strategies are available for research"
        )
    return await execute_backtest(request, app_settings, session, user, selected, controls, execution_controls)


async def execute_backtest(
    request: BacktestRunRequest,
    app_settings,  # noqa: ANN001
    session,  # noqa: ANN001
    user: User,
    selected: list[StrategyConfiguration],
    controls: dict,
    execution_controls: PaperExecutionControls,
) -> BacktestRunDetail:
    """Replay ``selected`` over the stored candles and persist the run.

    Shared by Backtesting and by the strategy builder, so a draft strategy is
    measured by exactly the code that measures a saved one.
    """
    try:
        for item in selected:
            StrategyRegistry.definition(item.strategy_type)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    # Run by the research worker, not here. The request carries everything
    # the run needs, frozen now: a settings change made while it waits does not
    # change what it measures. The indicator periods are read by the worker at
    # run time through the scanner's own call.
    payload = research_runs.backtest_payload(request.model_dump(mode="json"), selected, controls, execution_controls)
    finished = await _run_as_job(
        session, app_settings, research_runs.BACKTEST, payload, user, wait_seconds=BACKTEST_WAIT_SECONDS
    )
    run = await session.get(BacktestRun, UUID(finished.result["backtest_run_id"]))
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="The finished backtest run was not found")
    trades = list(
        (
            await session.scalars(
                select(BacktestTrade).where(BacktestTrade.run_id == run.id).order_by(BacktestTrade.exited_at)
            )
        ).all()
    )
    return BacktestRunDetail(**_run_response(run).model_dump(), trades=[_trade_response(item) for item in trades])


class SweepRequest(BaseModel):
    strategy_id: str
    start_date: date
    end_date: date
    instrument_tokens: list[str] = Field(min_length=1, max_length=5)
    timeframe_seconds: int = Field(default=60, ge=60, le=900)
    validation_fraction: float = Field(default=0.35, ge=0.1, le=0.6)
    parameter_grid: dict[str, list[float]] = Field(min_length=1)
    dataset_id: UUID | None = None

    @field_validator("instrument_tokens")
    @classmethod
    def normalize_tokens(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(item.strip() for item in values if item.strip()))
        if not normalized:
            raise ValueError("At least one instrument token is required")
        return normalized

    @field_validator("parameter_grid")
    @classmethod
    def validate_grid(cls, grid: dict[str, list[float]]) -> dict[str, list[float]]:
        unknown = sorted(set(grid) - SWEEPABLE)
        if unknown:
            raise ValueError(f"Unsupported sweep parameter(s): {', '.join(unknown)}")
        cleaned = {key: list(dict.fromkeys(values)) for key, values in grid.items() if values}
        if not cleaned:
            raise ValueError("Every grid entry is empty")
        return cleaned

    @model_validator(mode="after")
    def validate_range(self) -> "SweepRequest":
        if self.end_date < self.start_date:
            raise ValueError("end_date must not precede start_date")
        if (self.end_date - self.start_date).days > 60:
            raise ValueError("Sweeps are limited to 61 calendar days")
        return self


class SweepCombinationResponse(BaseModel):
    index: int
    parameters: dict
    in_sample: dict
    validation: dict
    proven: bool


class SweepResponse(BaseModel):
    id: str
    status: str
    strategy_id: str
    start_date: date
    end_date: date
    validation_fraction: float
    instrument_tokens: list[str]
    parameter_grid: dict
    combination_count: int
    best_index: int | None
    promoted_index: int | None
    combinations: list[SweepCombinationResponse]
    failure_detail: str | None
    created_at: datetime


def _sweep_response(row: BacktestSweep) -> SweepResponse:
    return SweepResponse(
        id=str(row.id),
        status=row.status,
        strategy_id=row.strategy_id,
        start_date=row.start_date,
        end_date=row.end_date,
        validation_fraction=float(row.validation_fraction),
        instrument_tokens=row.instrument_tokens,
        parameter_grid=row.parameter_grid,
        combination_count=row.combination_count,
        best_index=row.best_index,
        promoted_index=row.promoted_index,
        combinations=[SweepCombinationResponse(**item) for item in row.combinations],
        failure_detail=row.failure_detail,
        created_at=row.created_at,
    )


async def _load_strategies(session: DbSession) -> list[StrategyConfiguration]:
    strategies_setting = await session.get(ApplicationSetting, STRATEGIES_KEY)
    return [
        StrategyConfiguration.model_validate(item)
        for item in (strategies_setting.value if strategies_setting else DEFAULT_STRATEGIES)
    ]


@router.get("/sweeps", response_model=list[SweepResponse])
async def list_sweeps(
    _: CurrentUser, session: DbSession, limit: int = Query(default=20, ge=1, le=100)
) -> list[SweepResponse]:
    rows = list(
        (await session.scalars(select(BacktestSweep).order_by(BacktestSweep.created_at.desc()).limit(limit))).all()
    )
    return [_sweep_response(row) for row in rows]


@router.get("/sweeps/{sweep_id}", response_model=SweepResponse)
async def get_sweep(sweep_id: UUID, _: CurrentUser, session: DbSession) -> SweepResponse:
    row = await session.get(BacktestSweep, sweep_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Backtest sweep not found")
    return _sweep_response(row)


@router.post("/sweeps", response_model=SweepResponse, status_code=status.HTTP_201_CREATED)
async def create_sweep(
    request: SweepRequest,
    app_settings: AppSettings,
    session: DbSession,
    user: User = Depends(require_roles(UserRole.ADMIN)),
) -> SweepResponse:
    strategies = await _load_strategies(session)
    base_strategy = next((item for item in strategies if item.id == request.strategy_id), None)
    if base_strategy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Base strategy was not found")
    trading_setting = await session.get(ApplicationSetting, TRADING_KEY)
    controls = TradingControls.model_validate(
        trading_setting.value if trading_setting else DEFAULT_TRADING_CONTROLS
    ).model_dump()
    execution_setting = await session.get(ApplicationSetting, PAPER_EXECUTION_KEY)
    execution_controls = PaperExecutionControls.model_validate(
        execution_setting.value if execution_setting else DEFAULT_PAPER_EXECUTION_CONTROLS
    )
    # Run by the research worker; see execute_backtest.
    payload = research_runs.sweep_payload(
        request.model_dump(mode="json"),
        base_strategy,
        controls,
        execution_controls,
        app_settings.backtest_sweep_max_combinations,
    )
    finished = await _run_as_job(
        session, app_settings, research_runs.SWEEP, payload, user, wait_seconds=SWEEP_WAIT_SECONDS
    )
    sweep = await session.get(BacktestSweep, UUID(finished.result["sweep_id"]))
    if sweep is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="The finished sweep was not found")
    return _sweep_response(sweep)


# The only strategy fields a sweep result may write. A combination is stored
# JSON, so it is filtered against what a sweep can actually vary rather than
# against every field on the model: a stored row carrying ``enabled``,
# ``live_orders`` or ``id`` must never switch a strategy on, make it live, or
# move its rules onto another strategy.
PROMOTABLE_FIELDS = frozenset(STRATEGY_PARAMS)

LIVE_PROMOTION_REFUSED = (
    "This strategy is LIVE and trades real money. A sweep result cannot change a live strategy. "
    "Switch it to Paper first, or make the change in Strategies > Builder, which asks for "
    "confirmation and a reason."
)


def promotion_refusal(strategy: StrategyConfiguration | None) -> str | None:
    """Why a sweep result may not be written to this strategy, or None if it may.

    A LIVE strategy is refused outright. Promotion is a one-click write from a
    research screen, and what it writes reaches real orders on the next candle.
    The Strategy Builder is the path that asks for confirmation and a reason
    before touching a live strategy; promotion asks for neither.
    """
    if strategy is None:
        return "The strategy this sweep was run on no longer exists."
    if strategy_mode(strategy) == "LIVE":
        return LIVE_PROMOTION_REFUSED
    return None


@router.post("/sweeps/{sweep_id}/promote", response_model=list[StrategyConfiguration])
async def promote_sweep_combination(
    sweep_id: UUID,
    session: DbSession,
    combination_index: int = Query(ge=0),
    reason: str | None = Query(default=None, max_length=300),
    user: User = Depends(require_roles(UserRole.ADMIN)),
) -> list[StrategyConfiguration]:
    """Write one sweep combination to an Off or Paper strategy, never a Live one.

    Goes through ``_persist_strategies``, the same write path as every other
    strategy change, so the save is versioned, recorded in setting_revisions
    and audited. It used to write ``paper_strategies`` directly, which skipped
    both the revision and the live-strategy confirmation.
    """
    sweep = await session.get(BacktestSweep, sweep_id)
    if sweep is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Backtest sweep not found")
    combination = next(
        (
            item
            for item in (sweep.combinations or [])
            if isinstance(item, dict) and item.get("index") == combination_index
        ),
        None,
    )
    if combination is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Combination index is out of range")
    parameters = combination.get("parameters")
    if not isinstance(parameters, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="This combination has no readable parameters."
        )

    overrides = {key: value for key, value in parameters.items() if key in PROMOTABLE_FIELDS}
    if not overrides:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This combination only varies trading-control, indicator or exit parameters, which "
            "cannot be promoted here.",
        )

    previous = await _stored_strategies(session)
    current = previous.get(sweep.strategy_id)
    refusal = promotion_refusal(current)
    if refusal is not None:
        code = status.HTTP_404_NOT_FOUND if current is None else status.HTTP_409_CONFLICT
        raise HTTPException(status_code=code, detail=refusal)

    try:
        candidate = StrategyConfiguration.model_validate({**current.model_dump(), **overrides})
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    # Whatever the stored combination held, the mode stays what it was. A
    # promotion changes rules; it never switches Off, Paper or Live.
    candidate = candidate.model_copy(update={"enabled": current.enabled, "live_orders": current.live_orders})
    if candidate.model_dump(exclude={"version"}) == current.model_dump(exclude={"version"}):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This combination matches the strategy's current parameters"
        )

    sweep.promoted_index = combination_index
    return await _persist_strategies(
        session,
        [candidate if item.id == current.id else item for item in previous.values()],
        previous,
        user,
        "backtest.sweep_promoted",
        {
            "sweep_id": str(sweep.id),
            "combination_index": combination_index,
            "strategy_id": current.id,
            "mode": strategy_mode(current),
            "parameters": overrides,
            "reason": (reason or "").strip() or None,
        },
    )


@router.get("/{run_id}", response_model=BacktestRunDetail)
async def get_backtest(run_id: UUID, _: CurrentUser, session: DbSession) -> BacktestRunDetail:
    run = await session.get(BacktestRun, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Backtest run not found")
    trades = list(
        (
            await session.scalars(
                select(BacktestTrade).where(BacktestTrade.run_id == run.id).order_by(BacktestTrade.exited_at)
            )
        ).all()
    )
    return BacktestRunDetail(**_run_response(run).model_dump(), trades=[_trade_response(item) for item in trades])
