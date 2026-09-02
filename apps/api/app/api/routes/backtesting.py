"""Completed-candle-only historical research APIs; these routes cannot submit orders."""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select

from app.api.deps import AppSettings, CurrentUser, DbSession, require_roles
from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY, TradingControls
from app.db.models import ApplicationSetting, AuditLog, BacktestRun, BacktestTrade, MarketCandle, User, UserRole
from app.services.backtesting import run_completed_candle_backtest
from app.services.market_calculations import CompletedCandle
from app.services.paper_execution import DEFAULT_PAPER_EXECUTION_CONTROLS, PAPER_EXECUTION_KEY, PaperExecutionControls
from app.services.strategy_registry import DEFAULT_STRATEGIES, STRATEGIES_KEY, StrategyConfiguration, StrategyRegistry

router = APIRouter(prefix="/backtests", tags=["Backtesting"])


class BacktestRunRequest(BaseModel):
    start_date: date
    end_date: date
    instrument_tokens: list[str] = Field(min_length=1, max_length=10)
    strategy_ids: list[str] = Field(default_factory=list, max_length=10)
    timeframe_seconds: int = Field(default=60, ge=60, le=900)

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


def _completed(row: MarketCandle) -> CompletedCandle:
    return CompletedCandle(
        instrument_token=row.instrument_token,
        timeframe_seconds=row.timeframe_seconds,
        opened_at=row.opened_at,
        closed_at=row.closed_at,
        open=Decimal(str(row.open)),
        high=Decimal(str(row.high)),
        low=Decimal(str(row.low)),
        close=Decimal(str(row.close)),
        volume=row.volume,
        tick_count=row.tick_count,
    )


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
    try:
        for item in selected:
            StrategyRegistry.definition(item.strategy_type)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    all_tokens = list(dict.fromkeys([*request.instrument_tokens, app_settings.nifty_benchmark_token]))
    rows = list(
        (
            await session.scalars(
                select(MarketCandle)
                .where(
                    MarketCandle.instrument_token.in_(all_tokens),
                    MarketCandle.timeframe_seconds == request.timeframe_seconds,
                    MarketCandle.session_date >= request.start_date,
                    MarketCandle.session_date <= request.end_date,
                )
                .order_by(MarketCandle.instrument_token, MarketCandle.opened_at)
            )
        ).all()
    )
    by_instrument: dict[str, list[CompletedCandle]] = {item: [] for item in request.instrument_tokens}
    benchmark: list[CompletedCandle] = []
    for row in rows:
        candle = _completed(row)
        if row.instrument_token == app_settings.nifty_benchmark_token:
            benchmark.append(candle)
        elif row.instrument_token in by_instrument:
            by_instrument[row.instrument_token].append(candle)
    if not benchmark:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Benchmark candles are unavailable for this range"
        )
    if not any(by_instrument.values()):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="No requested historical candles are available"
        )
    result = run_completed_candle_backtest(
        by_instrument, benchmark, selected, controls, execution_controls, app_settings
    )
    run = BacktestRun(
        created_by_user_id=user.id,
        status="COMPLETED",
        start_date=request.start_date,
        end_date=request.end_date,
        timeframe_seconds=request.timeframe_seconds,
        instrument_tokens=request.instrument_tokens,
        strategy_snapshot=[item.model_dump(mode="json") for item in selected],
        controls_snapshot=controls,
        execution_snapshot=execution_controls.model_dump(mode="json"),
        data_fingerprint=result.data_fingerprint,
        source_candle_count=result.source_candle_count,
        initial_capital=Decimal(str(result.summary["initial_capital"])),
        final_equity=Decimal(str(result.summary["final_equity"])),
        net_pnl=Decimal(str(result.summary["net_pnl"])),
        max_drawdown=Decimal(str(result.summary["max_drawdown"])),
        result_summary=result.summary,
    )
    session.add(run)
    await session.flush()
    for trade in result.trades:
        session.add(
            BacktestTrade(
                run_id=run.id,
                trade_key=trade.trade_key,
                strategy_id=trade.strategy_id,
                strategy_name=trade.strategy_name,
                strategy_version=trade.strategy_version,
                instrument_token=trade.instrument_token,
                session_date=date.fromisoformat(trade.session_date),
                side=trade.side,
                quantity=trade.quantity,
                signal_at=trade.signal_at,
                entered_at=trade.entered_at,
                exited_at=trade.exited_at,
                entry_price=trade.entry_price,
                exit_price=trade.exit_price,
                gross_pnl=trade.gross_pnl,
                fees_total=trade.fees_total,
                net_pnl=trade.net_pnl,
                realized_r=trade.realized_r,
                exit_reason=trade.exit_reason,
            )
        )
    session.add(
        AuditLog(
            user_id=user.id,
            event_type="backtest.completed",
            metadata_json={
                "run_id": str(run.id),
                "trades": len(result.trades),
                "data_fingerprint": result.data_fingerprint,
            },
        )
    )
    await session.commit()
    await session.refresh(run)
    trades = list(
        (
            await session.scalars(
                select(BacktestTrade).where(BacktestTrade.run_id == run.id).order_by(BacktestTrade.exited_at)
            )
        ).all()
    )
    return BacktestRunDetail(**_run_response(run).model_dump(), trades=[_trade_response(item) for item in trades])


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
