"""Run one research job and write its result. Called by the research worker, never by the API.

Moved here from ``api/routes/backtesting.py``, where the API ran the replay in a
thread. A thread that holds the GIL for an hour slows everything else in that
process, and the API process also runs the scheduler jobs that square live
positions off and reconcile them. Now the API writes a ``research_jobs`` row and
waits; ``app.research_worker`` claims it, runs it in its own container, and calls
``run_job``.

Every input is read from the job's ``payload``, which the API filled in when the
job was asked for -- strategies, trading controls, charges. The one thing read
at run time is the indicator periods, through ``indicator_settings.effective``,
the same call the scanner uses, so the replay measures with the scanner's ruler.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, BacktestRun, BacktestSweep, BacktestTrade, MarketCandle
from app.services import watchlist_store
from app.services.backtest_sweep import run_parameter_sweep
from app.services.indicator_settings import effective as effective_settings
from app.services.market_calculations import CompletedCandle
from app.services.paper_execution import PaperExecutionControls
from app.services.portfolio_simulation import run_portfolio_backtest
from app.services.sectors import sector_of
from app.services.strategy_registry import StrategyConfiguration, StrategyRegistry
from app.services.trading_symbols import resolve_script_names

BACKTEST = "backtest"
SWEEP = "sweep"
BACKFILL = "backfill"
JOB_TYPES = (BACKTEST, SWEEP, BACKFILL)


class ResearchInputError(ValueError):
    """The job cannot run on what it was given: no candles, no benchmark, an unknown strategy."""


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


async def load_history(
    session: AsyncSession,
    app_settings: Any,
    instrument_tokens: list[str],
    timeframe_seconds: int,
    start_date: date,
    end_date: date,
) -> tuple[dict[str, list[CompletedCandle]], list[CompletedCandle]]:
    """The requested shares' candles and the longest NIFTY series stored for the range."""
    # NIFTY is stored under whichever key the feed that recorded it uses:
    # "NSE:26000" from Firstock, "NSE_INDEX|Nifty 50" from Upstox. Asking for
    # both and letting the data decide is the only version of this that keeps
    # working when the deployment changes feed, or holds history from both.
    benchmark_keys = {app_settings.nifty_benchmark_token, app_settings.upstox_nifty_benchmark_key}
    # And the benchmark chosen in Strategies > Subscriptions, when one is saved.
    saved = await watchlist_store.load(session)
    if saved is not None:
        benchmark_keys.add(saved.benchmark)
    all_tokens = list(dict.fromkeys([*instrument_tokens, *benchmark_keys]))
    rows = list(
        (
            await session.scalars(
                select(MarketCandle)
                .where(
                    MarketCandle.instrument_token.in_(all_tokens),
                    MarketCandle.timeframe_seconds == timeframe_seconds,
                    MarketCandle.session_date >= start_date,
                    MarketCandle.session_date <= end_date,
                )
                .order_by(MarketCandle.instrument_token, MarketCandle.opened_at)
            )
        ).all()
    )
    by_instrument: dict[str, list[CompletedCandle]] = {item: [] for item in instrument_tokens}
    # Keyed per benchmark candidate rather than merged: two feeds' NIFTY series
    # interleaved would be one series with two of every candle.
    benchmark_series: dict[str, list[CompletedCandle]] = {key: [] for key in benchmark_keys}
    for row in rows:
        candle = _completed(row)
        if row.instrument_token in benchmark_series and row.instrument_token not in by_instrument:
            benchmark_series[row.instrument_token].append(candle)
        elif row.instrument_token in by_instrument:
            by_instrument[row.instrument_token].append(candle)
    benchmark = max(benchmark_series.values(), key=len, default=[])
    if not benchmark:
        raise ResearchInputError("Benchmark candles are unavailable for this range")
    if not any(by_instrument.values()):
        raise ResearchInputError("No requested historical candles are available")
    return by_instrument, benchmark


async def sector_lookup(session: AsyncSession, tokens: list[str]) -> Callable[[str], str]:
    """Each token's sector group, resolved once, for the replay's per-sector ceiling.

    Names, not tokens, because ``sectors`` is keyed by trading symbol, as the
    scanner reads it. A lookup that fails leaves every share in a group of its
    own -- the scanner's own fallback: the ceiling stops protecting and never
    starts refusing.
    """
    try:
        names = await resolve_script_names(session, tokens)
    except Exception:  # noqa: BLE001 - a name lookup must not stop a backtest
        names = {}
    groups = {token: sector_of(names.get(token, token)) for token in tokens}
    return lambda token: groups.get(token, sector_of(token))


async def candles_for(
    session: AsyncSession, settings: Any, request: dict, tokens: list[str], timeframe: int, start: date, end: date
) -> tuple[dict[str, list[CompletedCandle]], list[CompletedCandle], dict]:
    """The candles a run measures: a research dataset when the request names one, else the live store.

    Returns a provenance block for the result summary, so every run says which
    data it ran on -- a dataset by id and content hash, or the live table.
    """
    dataset_id = request.get("dataset_id")
    if not dataset_id:
        by_instrument, benchmark = await load_history(session, settings, tokens, timeframe, start, end)
        return by_instrument, benchmark, {"source": "public.market_candles"}
    from app.services.research_data import TIMEFRAMES, DatasetTooLarge, read_dataset

    try:
        dataset, by_instrument, benchmark = await read_dataset(session, UUID(str(dataset_id)), tokens, start, end)
    except DatasetTooLarge as exc:
        raise ResearchInputError(str(exc)) from exc
    if TIMEFRAMES[dataset.timeframe][2] != timeframe:
        raise ResearchInputError(
            f"Dataset {dataset.name} holds {dataset.timeframe} bars; this run asked for {timeframe}-second candles"
        )
    if dataset.status != "COMPLETE":
        raise ResearchInputError(
            f"Dataset {dataset.name} is {dataset.status}; only a finalised dataset can be measured"
        )
    if not benchmark:
        raise ResearchInputError("Benchmark candles are unavailable in this dataset for this range")
    if not any(by_instrument.values()):
        raise ResearchInputError("No requested instrument has bars in this dataset for this range")
    return (
        by_instrument,
        benchmark,
        {
            "sessions": sorted({candle.session_date for candle in benchmark}),
            "source": "research.dataset",
            "dataset_id": str(dataset.id),
            "dataset_name": dataset.name,
            "content_sha256": dataset.content_sha256,
            "price_adjustment": dataset.price_adjustment,
        },
    )


def _calendar_for(data_source: dict):  # noqa: ANN202 - a context manager
    """A dataset replay needs the dataset's own sessions as its calendar; the live store does not."""
    from contextlib import nullcontext

    from app.services.research_data import replay_calendar

    sessions = data_source.get("sessions")
    return replay_calendar(sessions) if sessions else nullcontext()


def _summary_of(data_source: dict) -> dict:
    """The provenance block as stored: the session list is summarised, not kept."""
    sessions = data_source.get("sessions")
    if not sessions:
        return data_source
    rest = {key: value for key, value in data_source.items() if key != "sessions"}
    return {
        **rest,
        "sessions": len(sessions),
        "first_session": sessions[0].isoformat(),
        "last_session": sessions[-1].isoformat(),
    }


# --- payloads: everything a job needs, frozen when it is asked for ----------------------


def backtest_payload(
    request: dict,
    strategies: list[StrategyConfiguration],
    controls: dict,
    execution_controls: PaperExecutionControls,
) -> dict:
    return {
        "request": _jsonable(request),
        "strategies": [item.model_dump(mode="json") for item in strategies],
        "controls": _jsonable(controls),
        "execution": execution_controls.model_dump(mode="json"),
    }


def sweep_payload(
    request: dict,
    strategy: StrategyConfiguration,
    controls: dict,
    execution_controls: PaperExecutionControls,
    maximum_combinations: int,
) -> dict:
    return {
        "request": _jsonable(request),
        "strategy": strategy.model_dump(mode="json"),
        "controls": _jsonable(controls),
        "execution": execution_controls.model_dump(mode="json"),
        "maximum_combinations": maximum_combinations,
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


# --- the two jobs ---------------------------------------------------------------------


async def run_backtest_job(session: AsyncSession, payload: dict, settings: Any, user_id: UUID | None) -> dict:
    request = payload["request"]
    selected = [StrategyConfiguration.model_validate(item) for item in payload["strategies"]]
    try:
        for item in selected:
            StrategyRegistry.definition(item.strategy_type)
    except ValueError as exc:
        raise ResearchInputError(str(exc)) from exc
    controls = payload["controls"]
    execution_controls = PaperExecutionControls.model_validate(payload["execution"])
    start, end = date.fromisoformat(request["start_date"]), date.fromisoformat(request["end_date"])
    tokens = list(request["instrument_tokens"])
    timeframe = int(request.get("timeframe_seconds", 60))

    measured_with = await effective_settings(session, settings)
    by_instrument, benchmark, data_source = await candles_for(
        session, measured_with, request, tokens, timeframe, start, end
    )
    sector_of_token = await sector_lookup(session, list(by_instrument))
    with _calendar_for(data_source):
        result, _ = run_portfolio_backtest(
            by_instrument,
            benchmark,
            selected,
            controls,
            execution_controls,
            measured_with,
            sector_of_token=sector_of_token,
        )
    data_source = _summary_of(data_source)
    run = BacktestRun(
        created_by_user_id=user_id,
        status="COMPLETED",
        start_date=start,
        end_date=end,
        timeframe_seconds=timeframe,
        instrument_tokens=tokens,
        strategy_snapshot=[item.model_dump(mode="json") for item in selected],
        controls_snapshot=controls,
        execution_snapshot=execution_controls.model_dump(mode="json"),
        data_fingerprint=result.data_fingerprint,
        source_candle_count=result.source_candle_count,
        initial_capital=Decimal(str(result.summary["initial_capital"])),
        final_equity=Decimal(str(result.summary["final_equity"])),
        net_pnl=Decimal(str(result.summary["net_pnl"])),
        max_drawdown=Decimal(str(result.summary["max_drawdown"])),
        result_summary={**result.summary, "data": data_source},
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
            user_id=user_id,
            event_type="backtest.completed",
            metadata_json={
                "run_id": str(run.id),
                "trades": len(result.trades),
                "data_fingerprint": result.data_fingerprint,
            },
        )
    )
    await session.flush()
    return {"backtest_run_id": str(run.id), "trades": len(result.trades)}


async def run_sweep_job(session: AsyncSession, payload: dict, settings: Any, user_id: UUID | None) -> dict:
    request = payload["request"]
    base_strategy = StrategyConfiguration.model_validate(payload["strategy"])
    controls = payload["controls"]
    execution_controls = PaperExecutionControls.model_validate(payload["execution"])
    start, end = date.fromisoformat(request["start_date"]), date.fromisoformat(request["end_date"])
    tokens = list(request["instrument_tokens"])
    timeframe = int(request.get("timeframe_seconds", 60))

    # The stored indicator periods, as the single backtest and the scanner use.
    measured_with = await effective_settings(session, settings)
    by_instrument, benchmark, data_source = await candles_for(
        session, measured_with, request, tokens, timeframe, start, end
    )
    sector_of_token = await sector_lookup(session, list(by_instrument))
    try:
        with _calendar_for(data_source):
            sweep_result = run_parameter_sweep(
                base_strategy,
                controls,
                execution_controls,
                measured_with,
                by_instrument,
                benchmark,
                request["parameter_grid"],
                validation_fraction=float(request.get("validation_fraction", 0.35)),
                maximum_combinations=int(payload.get("maximum_combinations", 40)),
                sector_of_token=sector_of_token,
            )
    except ValueError as exc:
        raise ResearchInputError(str(exc)) from exc
    data_source = _summary_of(data_source)

    combinations = [item.as_dict() for item in sweep_result.combinations]
    sweep = BacktestSweep(
        created_by_user_id=user_id,
        status="COMPLETED",
        strategy_id=base_strategy.id,
        strategy_snapshot=base_strategy.model_dump(mode="json"),
        start_date=start,
        end_date=end,
        timeframe_seconds=timeframe,
        validation_fraction=Decimal(str(request.get("validation_fraction", 0.35))),
        instrument_tokens=tokens,
        parameter_grid=request["parameter_grid"],
        combination_count=len(combinations),
        combinations=combinations,
        best_index=sweep_result.best_index,
    )
    session.add(sweep)
    await session.flush()
    session.add(
        AuditLog(
            user_id=user_id,
            event_type="backtest.sweep_completed",
            metadata_json={
                "sweep_id": str(sweep.id),
                "strategy_id": base_strategy.id,
                "combinations": len(combinations),
                "variants_tested": sweep_result.variants_tested,
                "data": data_source,
            },
        )
    )
    await session.flush()
    return {"sweep_id": str(sweep.id), "combinations": len(combinations)}


async def run_backfill_job(session: AsyncSession, payload: dict, settings: Any, user_id: UUID | None) -> dict:
    """Fetch, store and check a historical dataset; finalise it when every file is in.

    Stops between files when the research window closes and reports how far it
    got; running the same job again continues from there.
    """
    from app.services import research_data
    from app.services.research_jobs import research_allowed
    from app.services.trading_calendar import TradingCalendar

    calendar = TradingCalendar.from_settings(settings)
    try:
        dataset = await research_data.open_dataset(
            session,
            name=payload["dataset"],
            timeframe=payload["timeframe"],
            start=date.fromisoformat(payload["start"]),
            end=date.fromisoformat(payload["end"]),
            benchmark_key=payload.get("benchmark") or research_data.NIFTY_50,
            provenance={"instruments_requested": len(payload["instruments"])},
        )
    except ValueError as exc:
        raise ResearchInputError(str(exc)) from exc
    await session.commit()
    source = research_data.UpstoxPublicHistory(requests_per_second=float(payload.get("requests_per_second", 2.0)))
    report = await research_data.backfill(
        session,
        dataset,
        list(payload["instruments"]),
        source,
        keep_going=lambda: research_allowed(datetime.now(UTC), calendar).open,
    )
    result = {"dataset_id": str(dataset.id), "dataset": dataset.name, **report.__dict__}
    if report.stopped_early is None:
        result["coverage_rows"] = await research_data.compute_coverage(session, dataset)
        if payload.get("finalize", True):
            result["content_sha256"] = await research_data.finalize(session, dataset)
    return result


HANDLERS: dict[str, Callable[[AsyncSession, dict, Any, UUID | None], Awaitable[dict]]] = {
    BACKTEST: run_backtest_job,
    SWEEP: run_sweep_job,
    BACKFILL: run_backfill_job,
}


def _register_experiment_handlers() -> None:
    # Imported late: research_experiments imports this module's error type.
    from app.services import research_experiments

    HANDLERS[research_experiments.SELECTION_JOB] = research_experiments.selection_job
    HANDLERS[research_experiments.HOLDOUT_JOB] = research_experiments.holdout_job


_register_experiment_handlers()
