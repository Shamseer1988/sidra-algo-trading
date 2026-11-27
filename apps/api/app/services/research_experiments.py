"""Pre-registered experiments: decide what counts before looking, then look once.

The lifecycle, and what each step is not allowed to do:

* **DRAFT** -- the experiment is written down: hypothesis, dataset, instruments,
  base strategy, parameter grid (and so the number of variants), the selection
  period, the sealed holdout period after it, the walk-forward window sizes,
  and the mean R the holdout must reach. Editable.
* **QUEUED** (``register``) -- frozen. ``registered_at`` is set and nothing that
  decides the answer may change again.
* **SELECTED** (selection job) -- the grid is searched on the selection period
  only. The holdout's bars are never loaded: the dataset is read up to
  ``selection_end``. Walk-forward folds re-choose on each training window and
  score the next; the stitched out-of-sample record is what the selection
  evidence is. The final parameters are the best in-sample on the whole
  selection period. Statistics account for how many variants were tried.
* **HOLDOUT_SCORED** (holdout job) -- the chosen parameters, and only those,
  are run once on the holdout period. ``holdout_used`` is set with the result,
  and a second holdout run is refused. ``verdict`` is PASSED only if the
  holdout's mean R reaches the registered threshold *and* its 95% interval is
  above zero.

A PASSED verdict is evidence for paper trading, not permission to trade.
Nothing here can change a strategy setting.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ResearchDataset, ResearchExperiment, ResearchExperimentResult, ResearchHypothesis
from app.services import research_statistics as stats
from app.services.backtest_sweep import (
    CONTROL_PARAMS,
    EXIT_PARAMS,
    STRATEGY_PARAMS,
    _metrics,
    _settings_namespace,
    build_combinations,
    run_parameter_sweep,
    validate_grid,
    walk_forward,
)
from app.services.build_info import source_fingerprint
from app.services.paper_execution import PaperExecutionControls
from app.services.portfolio_simulation import run_portfolio_backtest
from app.services.research_data import read_dataset, replay_calendar
from app.services.strategy_registry import StrategyConfiguration

DRAFT = "DRAFT"
QUEUED = "QUEUED"
SELECTED = "SELECTED"
HOLDOUT_QUEUED = "HOLDOUT_QUEUED"
HOLDOUT_SCORED = "HOLDOUT_SCORED"
FAILED_TO_RUN = "FAILED_TO_RUN"
PASSED, FAILED = "PASSED", "FAILED"

SELECTION_JOB = "experiment_selection"
HOLDOUT_JOB = "experiment_holdout"

# Calendar days of bars loaded before a period so daily indicators (ATR, CPR,
# prior close) exist on its first session. Trades in the warm-up are discarded.
WARMUP_DAYS = 70
MAXIMUM_VARIANTS = 60


class ExperimentError(ValueError):
    """The experiment cannot move to the requested step, and why."""


def _date(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


async def check_registration(session: AsyncSession, experiment: ResearchExperiment) -> int:
    """Everything that must hold before an experiment is frozen. Returns the variant count."""
    dataset = await session.get(ResearchDataset, experiment.dataset_id)
    if dataset is None:
        raise ExperimentError("The dataset does not exist.")
    if dataset.status != "COMPLETE":
        raise ExperimentError(f"Dataset {dataset.name} is {dataset.status}; only a finalised dataset can be used.")
    if dataset.timeframe != "1minute":
        raise ExperimentError("The strategies are built on 1-minute candles; use a 1-minute dataset.")
    if not experiment.instruments:
        raise ExperimentError("Name at least one instrument.")
    if not (experiment.selection_start < experiment.selection_end < experiment.holdout_start <= experiment.holdout_end):
        raise ExperimentError("The holdout must start after the selection period ends, and both must be non-empty.")
    if experiment.selection_start < dataset.start_date or experiment.holdout_end > dataset.end_date:
        raise ExperimentError(f"Both periods must lie inside the dataset ({dataset.start_date} to {dataset.end_date}).")
    if experiment.train_sessions < 5 or experiment.test_sessions < 1:
        raise ExperimentError("Walk-forward needs at least 5 training sessions and 1 test session.")
    try:
        validate_grid(experiment.parameter_grid)
        combos = build_combinations(experiment.parameter_grid, MAXIMUM_VARIANTS)
    except ValueError as exc:
        raise ExperimentError(str(exc)) from exc
    StrategyConfiguration.model_validate(experiment.strategy)
    return len(combos)


async def create_hypothesis(
    session: AsyncSession, *, title: str, statement: str, rationale: str | None, user_id
) -> ResearchHypothesis:  # noqa: ANN001
    hypothesis = ResearchHypothesis(title=title, statement=statement, rationale=rationale, created_by_user_id=user_id)
    session.add(hypothesis)
    await session.flush()
    return hypothesis


async def create_experiment(session: AsyncSession, **fields: Any) -> ResearchExperiment:
    """A DRAFT experiment, validated as if it were about to be registered."""
    experiment = ResearchExperiment(status=DRAFT, variants=0, **fields)
    session.add(experiment)
    await session.flush()
    experiment.variants = await check_registration(session, experiment)
    await session.flush()
    return experiment


async def register(session: AsyncSession, experiment_id: UUID, *, user_id) -> ResearchExperiment:  # noqa: ANN001
    """Freeze the experiment and queue its selection job."""
    from app.services import research_jobs

    experiment = await session.get(ResearchExperiment, experiment_id, with_for_update=True)
    if experiment is None:
        raise ExperimentError("Experiment not found.")
    if experiment.status != DRAFT:
        raise ExperimentError(f"Only a DRAFT experiment can be registered; this one is {experiment.status}.")
    experiment.variants = await check_registration(session, experiment)
    experiment.status = QUEUED
    experiment.registered_at = datetime.now(UTC)
    await research_jobs.enqueue(
        session, SELECTION_JOB, {"experiment_id": str(experiment.id)}, user_id=user_id, timeout_seconds=6 * 60 * 60
    )
    await session.flush()
    return experiment


async def request_holdout(session: AsyncSession, experiment_id: UUID, *, user_id) -> ResearchExperiment:  # noqa: ANN001
    from app.services import research_jobs

    experiment = await session.get(ResearchExperiment, experiment_id, with_for_update=True)
    if experiment is None:
        raise ExperimentError("Experiment not found.")
    if experiment.holdout_used:
        raise ExperimentError("The holdout has already been used. It is scored once; a second look is not a test.")
    if experiment.status != SELECTED:
        raise ExperimentError(f"The holdout is scored after selection; this experiment is {experiment.status}.")
    experiment.status = HOLDOUT_QUEUED
    await research_jobs.enqueue(
        session, HOLDOUT_JOB, {"experiment_id": str(experiment.id)}, user_id=user_id, timeout_seconds=2 * 60 * 60
    )
    await session.flush()
    return experiment


# --- applying one combination ----------------------------------------------------------


def apply_combination(
    strategy: StrategyConfiguration, controls: dict, settings: Any, combo: dict
) -> tuple[StrategyConfiguration, dict, SimpleNamespace]:
    """The strategy, controls and indicator settings one grid combination describes.

    The same split ``run_parameter_sweep`` makes: strategy fields onto the
    strategy, exit fields into its nested exit rules, account controls into the
    controls, indicator periods into the settings.
    """
    variant = strategy.model_copy(update={key: value for key, value in combo.items() if key in STRATEGY_PARAMS})
    exits = {key: value for key, value in combo.items() if key in EXIT_PARAMS}
    if exits:
        variant = variant.model_copy(update={"exit_rules": variant.exit_rules.model_copy(update=exits)})
    controls_variant = {**controls, **{key: value for key, value in combo.items() if key in CONTROL_PARAMS}}
    return variant, controls_variant, _settings_namespace(settings, combo)


def _in_period(trades: list, start: date, end: date) -> list:
    return [trade for trade in trades if start <= _date(trade.session_date) <= end]


def _trade_rows(trades: list, limit: int = 400) -> list[dict]:
    return [
        {
            "session_date": str(t.session_date),
            "instrument": t.instrument_token,
            "side": t.side,
            "entry": str(t.entry_price),
            "exit": str(t.exit_price),
            "net": str(t.net_pnl),
            "r": str(t.realized_r),
            "reason": t.exit_reason,
        }
        for t in trades[:limit]
    ]


async def _load(session: AsyncSession, experiment: ResearchExperiment, start: date, end: date):
    dataset, by_instrument, benchmark = await read_dataset(
        session, experiment.dataset_id, list(experiment.instruments), start, end
    )
    if not benchmark:
        raise ExperimentError("The dataset has no benchmark bars for this period.")
    return dataset, by_instrument, benchmark


# --- the selection job ------------------------------------------------------------------


async def run_selection(session: AsyncSession, payload: dict, settings: Any, user_id) -> dict:  # noqa: ANN001
    from app.services.indicator_settings import effective as effective_settings

    experiment = await session.get(ResearchExperiment, UUID(payload["experiment_id"]), with_for_update=True)
    if experiment is None or experiment.status != QUEUED:
        raise ExperimentError("The experiment is not waiting for selection.")
    measured_with = await effective_settings(session, settings)
    base = StrategyConfiguration.model_validate(experiment.strategy)
    execution = PaperExecutionControls.model_validate(experiment.execution)
    warm_start = experiment.selection_start - timedelta(days=WARMUP_DAYS)
    # Read up to selection_end and no further: the holdout is never loaded here.
    dataset, by_instrument, benchmark = await _load(session, experiment, warm_start, experiment.selection_end)
    sessions = sorted({candle.session_date for candle in benchmark})
    selection_sessions = [day for day in sessions if day >= experiment.selection_start]

    with replay_calendar(sessions):
        sweep = run_parameter_sweep(
            base,
            dict(experiment.controls),
            execution,
            measured_with,
            by_instrument,
            benchmark,
            experiment.parameter_grid,
            maximum_combinations=MAXIMUM_VARIANTS,
        )
    combos = build_combinations(experiment.parameter_grid, MAXIMUM_VARIANTS)
    capital = Decimal(str(experiment.controls["account_capital"]))
    trades_by_index = {
        index: _in_period(trades, experiment.selection_start, experiment.selection_end)
        for index, trades in sweep.trades_by_index.items()
    }
    per_variant = []
    for index, combo in enumerate(combos):
        per_variant.append({"index": index, "parameters": combo, **_metrics(trades_by_index.get(index, []), capital)})
    ranked = sorted(
        per_variant, key=lambda item: (item["return_percent"], item["net_pnl"], -item["index"]), reverse=True
    )
    chosen = ranked[0]

    folds = walk_forward(
        trades_by_index,
        selection_sessions,
        train_sessions=experiment.train_sessions,
        test_sessions=experiment.test_sessions,
        capital=capital,
    )
    stitched_trades = [
        trade
        for fold in folds.folds
        if fold.chosen_index is not None
        for trade in trades_by_index[fold.chosen_index]
        if fold.test_start <= _date(trade.session_date) <= fold.test_end
    ]
    directions = stats.benchmark_day_direction(benchmark)
    variance = stats.sharpe_variance([[t.realized_r for t in trades] for trades in trades_by_index.values()])
    summary = {
        "dataset": {"id": str(dataset.id), "name": dataset.name, "price_adjustment": dataset.price_adjustment},
        "selection_sessions": len(selection_sessions),
        "variants_tested": len(combos),
        "chosen": {"index": chosen["index"], "parameters": chosen["parameters"]},
        "per_variant": per_variant,
        "walk_forward": {
            "train_sessions": experiment.train_sessions,
            "test_sessions": experiment.test_sessions,
            "folds": [fold.as_dict() for fold in folds.folds],
            "stitched": stats.summary(t.realized_r for t in stitched_trades),
            "stitched_metrics": folds.stitched,
            "deflated_sharpe": stats.deflated_sharpe(
                [t.realized_r for t in stitched_trades], trials=len(combos), sharpe_variance=variance
            ),
            "cost_sensitivity": stats.cost_sensitivity(stitched_trades),
            "regimes": stats.regime_split(stitched_trades, directions),
        },
    }
    experiment.chosen_index = chosen["index"]
    experiment.chosen_parameters = chosen["parameters"]
    experiment.status = SELECTED
    session.add(
        ResearchExperimentResult(
            experiment_id=experiment.id,
            kind="SELECTION",
            job_id=UUID(payload["job_id"]) if payload.get("job_id") else None,
            code_fingerprint=source_fingerprint(),
            dataset_sha256=dataset.content_sha256 or "",
            summary=summary,
        )
    )
    await session.flush()
    return {"experiment_id": str(experiment.id), "status": SELECTED, "chosen_index": chosen["index"]}


# --- the holdout job, once --------------------------------------------------------------


async def run_holdout(session: AsyncSession, payload: dict, settings: Any, user_id) -> dict:  # noqa: ANN001
    from app.services.indicator_settings import effective as effective_settings

    experiment = await session.get(ResearchExperiment, UUID(payload["experiment_id"]), with_for_update=True)
    if experiment is None or experiment.status != HOLDOUT_QUEUED or experiment.holdout_used:
        raise ExperimentError("The holdout is not waiting to be scored, or has already been used.")
    measured_with = await effective_settings(session, settings)
    base = StrategyConfiguration.model_validate(experiment.strategy)
    strategy, controls, settings_variant = apply_combination(
        base, dict(experiment.controls), measured_with, experiment.chosen_parameters or {}
    )
    execution = PaperExecutionControls.model_validate(experiment.execution)
    dataset, by_instrument, benchmark = await _load(
        session, experiment, experiment.holdout_start - timedelta(days=WARMUP_DAYS), experiment.holdout_end
    )
    sessions = sorted({candle.session_date for candle in benchmark})
    with replay_calendar(sessions):
        result, outcome = run_portfolio_backtest(
            by_instrument, benchmark, [strategy], controls, execution, settings_variant
        )
    trades = _in_period(list(result.trades), experiment.holdout_start, experiment.holdout_end)
    summary_stats = stats.summary(t.realized_r for t in trades)
    interval = summary_stats.get("ci95_mean_r")
    mean = summary_stats.get("mean_r")
    passed = mean is not None and mean >= float(experiment.success_mean_r) and interval is not None and interval[0] > 0
    experiment.holdout_used = True
    experiment.verdict = PASSED if passed else FAILED
    experiment.status = HOLDOUT_SCORED
    capital = Decimal(str(experiment.controls["account_capital"]))
    summary = {
        "dataset": {"id": str(dataset.id), "name": dataset.name, "price_adjustment": dataset.price_adjustment},
        "parameters": experiment.chosen_parameters,
        "holdout": [experiment.holdout_start.isoformat(), experiment.holdout_end.isoformat()],
        "holdout_sessions": len([day for day in sessions if day >= experiment.holdout_start]),
        "summary": summary_stats,
        "metrics": _metrics(trades, capital),
        "deflated_sharpe": stats.deflated_sharpe([t.realized_r for t in trades], trials=experiment.variants),
        "cost_sensitivity": stats.cost_sensitivity(trades),
        "regimes": stats.regime_split(trades, stats.benchmark_day_direction(benchmark)),
        "portfolio": outcome.summary(),
        "threshold_mean_r": float(experiment.success_mean_r),
        "verdict": experiment.verdict,
        "verdict_rule": "mean R >= the registered threshold AND the 95% interval of mean R is above zero",
        "trades": _trade_rows(trades),
    }
    session.add(
        ResearchExperimentResult(
            experiment_id=experiment.id,
            kind="HOLDOUT",
            job_id=UUID(payload["job_id"]) if payload.get("job_id") else None,
            code_fingerprint=source_fingerprint(),
            dataset_sha256=dataset.content_sha256 or "",
            summary=summary,
        )
    )
    await session.flush()
    return {"experiment_id": str(experiment.id), "status": HOLDOUT_SCORED, "verdict": experiment.verdict}


async def results_of(session: AsyncSession, experiment_id: UUID) -> list[ResearchExperimentResult]:
    return list(
        (
            await session.scalars(
                select(ResearchExperimentResult)
                .where(ResearchExperimentResult.experiment_id == experiment_id)
                .order_by(ResearchExperimentResult.created_at)
            )
        ).all()
    )


async def _after_failure(session: AsyncSession, experiment_id: UUID, status: str) -> None:
    await session.rollback()
    experiment = await session.get(ResearchExperiment, experiment_id, with_for_update=True)
    if experiment is not None and not experiment.holdout_used:
        experiment.status = status
        await session.flush()


async def selection_job(session: AsyncSession, payload: dict, settings: Any, user_id) -> dict:  # noqa: ANN001
    """The selection job as the worker runs it: a failure leaves the experiment FAILED_TO_RUN, not stuck."""
    from app.services.research_runs import ResearchInputError

    try:
        return await run_selection(session, payload, settings, user_id)
    except Exception as exc:
        await _after_failure(session, UUID(payload["experiment_id"]), FAILED_TO_RUN)
        await session.commit()
        raise ResearchInputError(f"Selection failed: {type(exc).__name__}: {exc}") from exc


async def holdout_job(session: AsyncSession, payload: dict, settings: Any, user_id) -> dict:  # noqa: ANN001
    """The holdout job as the worker runs it.

    A crash before the result is written puts the experiment back to SELECTED:
    nobody has seen a holdout number, so the holdout is still unused and may be
    scored again.
    """
    from app.services.research_runs import ResearchInputError

    try:
        return await run_holdout(session, payload, settings, user_id)
    except Exception as exc:
        await _after_failure(session, UUID(payload["experiment_id"]), SELECTED)
        await session.commit()
        raise ResearchInputError(f"Holdout scoring failed: {type(exc).__name__}: {exc}") from exc
