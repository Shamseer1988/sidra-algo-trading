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
    validate_grid,
    walk_forward,
)
from app.services.build_info import source_fingerprint
from app.services.paper_execution import PaperExecutionControls
from app.services.research_data import benchmark_days, replay_calendar
from app.services.research_replay import ReplayGroup, replay_taken, warmup_days
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
# prior close) exist on its first session; ``research_replay.warmup_days``
# raises it when the stored indicator settings look back further. Sessions in
# the warm-up are history only and are never replayed.
WARMUP_DAYS = 70
MAXIMUM_VARIANTS = 60

# A job's time limit, from its size. Replay measured 1.6 s per share-session on a
# desktop core (28 Nov 2026); 4 s allows for a slower machine, and the limit is
# 1.5 times the estimate. The first selection got a fixed 6 hours for a ~13-hour
# run and would have been killed part-way.
SECONDS_PER_SHARE_SESSION = 4.0
MINIMUM_TIMEOUT_SECONDS = 2 * 60 * 60
MAXIMUM_TIMEOUT_SECONDS = 7 * 24 * 60 * 60


def _weekdays(start: date, end: date) -> int:
    days = (end - start).days + 1
    if days <= 0:
        return 0
    full_weeks, rest = divmod(days, 7)
    return full_weeks * 5 + sum((start.weekday() + offset) % 7 < 5 for offset in range(rest))


def job_timeout_seconds(instruments: int, start: date, end: date, passes: int = 1) -> int:
    """How long a replay of ``instruments`` shares from ``start`` to ``end`` may run, in seconds."""
    estimate = instruments * _weekdays(start, end) * SECONDS_PER_SHARE_SESSION * max(1, passes)
    return int(min(MAXIMUM_TIMEOUT_SECONDS, max(MINIMUM_TIMEOUT_SECONDS, estimate * 1.5)))


def _selection_passes(grid: dict, variants: int) -> int:
    """One shared replay when the grid varies only strategy fields, else one per variant (see ``_groups``)."""
    return 1 if variants > 1 and set(grid) <= STRATEGY_PARAMS else max(1, variants)


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
    timeout = job_timeout_seconds(
        len(experiment.instruments),
        experiment.selection_start,
        experiment.selection_end,
        _selection_passes(experiment.parameter_grid, experiment.variants),
    )
    await research_jobs.enqueue(
        session, SELECTION_JOB, {"experiment_id": str(experiment.id)}, user_id=user_id, timeout_seconds=timeout
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
    timeout = job_timeout_seconds(len(experiment.instruments), experiment.holdout_start, experiment.holdout_end)
    await research_jobs.enqueue(
        session, HOLDOUT_JOB, {"experiment_id": str(experiment.id)}, user_id=user_id, timeout_seconds=timeout
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


async def _dataset_and_days(
    session: AsyncSession, experiment: ResearchExperiment, start: date, end: date
) -> tuple[ResearchDataset, dict[date, str]]:
    """The experiment's dataset and its benchmark sessions from ``start`` to ``end`` ("UP"/"DOWN")."""
    dataset = await session.get(ResearchDataset, experiment.dataset_id)
    if dataset is None:
        raise ExperimentError("The experiment's dataset no longer exists.")
    days = await benchmark_days(session, dataset, start, end)
    if not days:
        raise ExperimentError("The dataset has no benchmark bars for this period.")
    return dataset, days


def _groups(
    base: StrategyConfiguration, controls: dict, settings: Any, grid: dict, combos: list[dict]
) -> list[ReplayGroup]:
    """The replays the grid needs: one shared pass when it varies only strategy fields, else one per combination.

    The same rule ``run_parameter_sweep`` follows: controls and indicator
    periods are fixed for a whole replay, so a combination changing them
    needs a replay of its own.
    """
    variants = []
    for index, combo in enumerate(combos):
        strategy, controls_variant, settings_variant = apply_combination(base, controls, settings, combo)
        strategy = strategy.model_copy(update={"id": f"{base.id}#sweep{index}"})
        variants.append((index, strategy, controls_variant, settings_variant))
    if len(combos) > 1 and set(grid) <= STRATEGY_PARAMS:
        return [
            ReplayGroup(
                strategies=[strategy for _, strategy, _, _ in variants],
                controls=dict(controls),
                settings=_settings_namespace(settings, {}),
                index_of={strategy.id: index for index, strategy, _, _ in variants},
            )
        ]
    return [
        ReplayGroup(
            strategies=[strategy], controls=controls_variant, settings=settings_variant, index_of={strategy.id: index}
        )
        for index, strategy, controls_variant, settings_variant in variants
    ]


# --- the selection job ------------------------------------------------------------------


async def run_selection(session: AsyncSession, payload: dict, settings: Any, user_id) -> dict:  # noqa: ANN001
    from app.services.indicator_settings import effective as effective_settings

    experiment = await session.get(ResearchExperiment, UUID(payload["experiment_id"]), with_for_update=True)
    if experiment is None or experiment.status != QUEUED:
        raise ExperimentError("The experiment is not waiting for selection.")
    measured_with = await effective_settings(session, settings)
    base = StrategyConfiguration.model_validate(experiment.strategy)
    execution = PaperExecutionControls.model_validate(experiment.execution)
    validate_grid(experiment.parameter_grid)
    combos = build_combinations(experiment.parameter_grid, MAXIMUM_VARIANTS)
    groups = _groups(base, dict(experiment.controls), measured_with, experiment.parameter_grid, combos)
    warm_start = experiment.selection_start - timedelta(days=max(warmup_days(group.settings) for group in groups))
    # Nothing after selection_end is read: the holdout is never loaded here.
    dataset, days = await _dataset_and_days(session, experiment, warm_start, experiment.selection_end)
    sessions = list(days)
    selection_sessions = [day for day in sessions if day >= experiment.selection_start]

    with replay_calendar(sessions):
        taken, replay_stats = await replay_taken(
            session,
            dataset,
            list(experiment.instruments),
            experiment.selection_start,
            experiment.selection_end,
            groups,
            execution,
        )
    capital = Decimal(str(experiment.controls["account_capital"]))
    trades_by_index = {
        index: _in_period(taken.get(index, []), experiment.selection_start, experiment.selection_end)
        for index in range(len(combos))
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
    directions = days
    variance = stats.sharpe_variance([[t.realized_r for t in trades] for trades in trades_by_index.values()])
    summary = {
        "dataset": {"id": str(dataset.id), "name": dataset.name, "price_adjustment": dataset.price_adjustment},
        "selection_sessions": len(selection_sessions),
        "replay": replay_stats.as_dict(),
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
    warm_start = experiment.holdout_start - timedelta(days=warmup_days(settings_variant))
    dataset, days = await _dataset_and_days(session, experiment, warm_start, experiment.holdout_end)
    sessions = list(days)
    group = ReplayGroup(strategies=[strategy], controls=controls, settings=settings_variant, index_of={strategy.id: 0})
    with replay_calendar(sessions):
        taken, replay_stats = await replay_taken(
            session,
            dataset,
            list(experiment.instruments),
            experiment.holdout_start,
            experiment.holdout_end,
            [group],
            execution,
        )
    trades = _in_period(taken.get(0, []), experiment.holdout_start, experiment.holdout_end)
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
        "regimes": stats.regime_split(trades, days),
        "portfolio": replay_stats.portfolio_summary(0),
        "replay": replay_stats.as_dict(),
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
