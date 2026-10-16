"""Deterministic parameter-grid search for one strategy with an out-of-sample holdout.

Each grid combination is backtested once over the full range; trades are then split by
session date into an in-sample block and a later validation block. Combinations are
ranked by validation return so a configuration that only looks good in-sample ranks low.
Nothing is promoted automatically.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import product
from types import SimpleNamespace

from app.services.backtesting import run_completed_candle_backtest
from app.services.exit_rules import ATR_TRAIL, BREAKEVEN_AT_R, NO_TRAIL
from app.services.market_calculations import CompletedCandle
from app.services.paper_execution import PaperExecutionControls
from app.services.strategy_registry import StrategyConfiguration

STRATEGY_PARAMS = {
    "minimum_score",
    "minimum_rr",
    "volume_multiplier",
    "retest_tolerance_percent",
    "minimum_ema_spread_percent",
    "rs_threshold_percent",
    "max_trades_per_day",
    "cooldown_minutes",
}
CONTROL_PARAMS = {
    "stop_atr_multiple",
    "min_stop_distance_percent",
    "risk_per_trade_percent",
    # When entries stop being taken. Already obeyed by every strategy through
    # _inside_trade_window, so sweeping it needs no second implementation.
    "trade_cutoff_time",
}
SETTINGS_PARAMS = {"opening_range_minutes", "ema_fast_period", "ema_slow_period", "atr_period"}
# Carried by the strategy, but nested one level down in ``exit_rules`` rather
# than sitting on the model itself -- so they cannot be applied by the flat
# model_copy that every other strategy parameter uses, and they are kept apart
# to make that difference visible rather than surprising.
#
# trailing_atr_multiple is deliberately absent: it only means anything to
# ATR_TRAIL, and ATR_TRAIL is refused below.
EXIT_PARAMS = {"trailing_rule", "trailing_trigger_r"}
SWEEPABLE = STRATEGY_PARAMS | CONTROL_PARAMS | SETTINGS_PARAMS | EXIT_PARAMS

# The trailing rules the live path will actually apply. live_exit_manager moves
# a stop only for BREAKEVEN_AT_R; ATR_TRAIL is logged and ignored, because
# moving a stop at the broker is a cancel and a replace, and a rule that moves
# most minutes of the day would leave the position unprotected again and again.
# Tuning a rule the account never executes is worse than not tuning at all, so
# the sweep refuses it rather than reporting a result nobody can trade.
LIVE_TRAILING_RULES = (NO_TRAIL, BREAKEVEN_AT_R)
_SETTINGS_FIELDS = (
    "opening_range_minutes",
    "ema_fast_period",
    "ema_slow_period",
    "volume_lookback_candles",
    "atr_period",
    "daily_history_sessions",
)


@dataclass(frozen=True)
class SweepCombination:
    index: int
    parameters: dict
    in_sample: dict
    validation: dict
    proven: bool

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "parameters": self.parameters,
            "in_sample": self.in_sample,
            "validation": self.validation,
            "proven": self.proven,
        }


@dataclass(frozen=True)
class SweepResult:
    combinations: tuple[SweepCombination, ...]
    best_index: int | None


def _normalise(value: object) -> object:
    """Integral values become ``int`` so they can index candle lists and EMA periods.

    Text passes through untouched. Every sweepable parameter used to be a
    number, so this coerced with float() -- which made a grid carrying
    ``trailing_rule=NONE`` or ``trade_cutoff_time=13:30`` raise here, inside
    the de-duplication, long before anything could explain why.
    """
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return value
    return int(number) if number.is_integer() else number


def validate_grid(grid: dict[str, list]) -> None:
    """Refuse a grid that would tune something the live account cannot use.

    Raises ValueError with the reason. Called before any replay starts, because
    the whole cost of a sweep is paid before a wrong answer is printed.
    """
    unknown = sorted(set(grid) - SWEEPABLE)
    if unknown:
        raise ValueError(f"Not sweepable: {', '.join(unknown)}. Sweepable: {', '.join(sorted(SWEEPABLE))}")

    for value in grid.get("trailing_rule", []):
        rule = str(value).strip().upper()
        if rule == ATR_TRAIL:
            raise ValueError(
                "trailing_rule=ATR_TRAIL cannot be swept: the live path never applies it. "
                "live_exit_manager moves a stop only for BREAKEVEN_AT_R, because moving one at "
                "the broker means cancelling the resting order and placing another, and a rule "
                "that moves most minutes of the day would leave the position unprotected over "
                "and over. A backtest of it would score an exit the account would never get. "
                f"Sweep {' or '.join(LIVE_TRAILING_RULES)} instead."
            )
        if rule not in LIVE_TRAILING_RULES:
            raise ValueError(f"trailing_rule={value!r} is not a rule: use {' or '.join(LIVE_TRAILING_RULES)}.")

    for value in grid.get("trade_cutoff_time", []):
        text = str(value).strip()
        try:
            hour, minute = (int(part) for part in text.split(":"))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except ValueError as error:
            raise ValueError(
                f"trade_cutoff_time={value!r} must be HH:MM in IST, for example 13:30. "
                "It is compared as text against each candle's IST time, so a different "
                "spelling would silently compare wrongly rather than fail."
            ) from error


def build_combinations(grid: dict[str, list], maximum: int) -> list[dict]:
    unknown = sorted(set(grid) - SWEEPABLE)
    if unknown:
        raise ValueError(f"Unsupported sweep parameter(s): {', '.join(unknown)}")
    cleaned = {key: list(dict.fromkeys(_normalise(item) for item in values)) for key, values in grid.items() if values}
    if not cleaned:
        raise ValueError("The parameter grid is empty")
    keys = sorted(cleaned)
    combos = [dict(zip(keys, choice, strict=True)) for choice in product(*(cleaned[key] for key in keys))]
    if len(combos) > maximum:
        raise ValueError(f"The grid expands to {len(combos)} combinations; the limit is {maximum}")
    return combos


def _settings_namespace(base_settings: object, overrides: dict) -> SimpleNamespace:
    values = {field: getattr(base_settings, field) for field in _SETTINGS_FIELDS}
    values.update({key: overrides[key] for key in overrides if key in _SETTINGS_FIELDS})
    return SimpleNamespace(**values)


def _partition_dates(dates: list[date], validation_fraction: float) -> tuple[set[date], set[date]]:
    ordered = sorted(dates)
    if len(ordered) < 2:
        return set(ordered), set()
    split = max(1, min(len(ordered) - 1, round(len(ordered) * (1.0 - validation_fraction))))
    return set(ordered[:split]), set(ordered[split:])


def _metrics(trades: list, capital: Decimal) -> dict:
    net = sum((trade.net_pnl for trade in trades), start=Decimal("0"))
    wins = sum(trade.net_pnl > 0 for trade in trades)
    losses = sum(trade.net_pnl < 0 for trade in trades)
    profit = sum((trade.net_pnl for trade in trades if trade.net_pnl > 0), start=Decimal("0"))
    loss = sum((-trade.net_pnl for trade in trades if trade.net_pnl < 0), start=Decimal("0"))
    realized_r = sum((trade.realized_r for trade in trades), start=Decimal("0"))
    return {
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate_percent": round(float(wins * 100 / len(trades)), 2) if trades else 0.0,
        "net_pnl": round(float(net), 2),
        "return_percent": round(float(net / capital * 100), 4) if capital else 0.0,
        "profit_factor": round(float(profit / loss), 4) if loss else None,
        "average_realized_r": round(float(realized_r / len(trades)), 4) if trades else 0.0,
    }


def _rank(results: list[SweepCombination]) -> int | None:
    """The index to recommend: the best proven combination, else the best row.

    Shared by both paths so they cannot disagree about what "best" means.
    """
    ranked = sorted(
        results,
        key=lambda item: (
            item.validation["return_percent"],
            item.validation["net_pnl"],
            item.in_sample["return_percent"],
        ),
        reverse=True,
    )
    return next((item.index for item in ranked if item.proven), ranked[0].index if ranked else None)


def _one_pass(
    base_strategy: StrategyConfiguration,
    base_controls: dict,
    execution_controls: PaperExecutionControls,
    base_settings: object,
    candles_by_instrument: dict[str, list[CompletedCandle]],
    benchmark_candles: list[CompletedCandle],
    combos: list[dict],
    in_sample_dates: set[date],
    validation_dates: set[date],
    capital: Decimal,
    progress: Callable[[int, int, dict], None] | None,
) -> SweepResult:
    """Every combination in one replay, which is the whole optimisation.

    Each variant gets a distinct id so its trades can be told apart again --
    the replay stamps ``strategy_id`` on every trade, and nothing in it lets
    one strategy's decisions reach another: state is per strategy, the trade
    list is append-only, and there is no cross-strategy daily cap in a replay.
    """
    variants = [
        base_strategy.model_copy(update={**combo, "id": f"{base_strategy.id}#sweep{index}"})
        for index, combo in enumerate(combos)
    ]
    if progress is not None:
        # One pass, so one report: the per-combination count would be a lie
        # about what is happening and the estimate drawn from it worse.
        progress(0, 1, {"combinations": len(combos), "shared_pass": True})

    result = run_completed_candle_backtest(
        candles_by_instrument,
        benchmark_candles,
        variants,
        base_controls,
        execution_controls,
        _settings_namespace(base_settings, {}),
    )
    by_variant: dict[str, list] = {variant.id: [] for variant in variants}
    for trade in result.trades:
        if trade.strategy_id in by_variant:
            by_variant[trade.strategy_id].append(trade)

    results: list[SweepCombination] = []
    for index, (combo, variant) in enumerate(zip(combos, variants, strict=True)):
        mine = by_variant[variant.id]
        in_trades = [t for t in mine if date.fromisoformat(t.session_date) in in_sample_dates]
        val_trades = [t for t in mine if date.fromisoformat(t.session_date) in validation_dates]
        in_metrics = _metrics(in_trades, capital)
        val_metrics = _metrics(val_trades, capital)
        proven = val_metrics["trades"] >= 3 and val_metrics["return_percent"] > 0
        results.append(SweepCombination(index, combo, in_metrics, val_metrics, proven))

    return SweepResult(combinations=tuple(results), best_index=_rank(results))


def run_parameter_sweep(
    base_strategy: StrategyConfiguration,
    base_controls: dict,
    execution_controls: PaperExecutionControls,
    base_settings: object,
    candles_by_instrument: dict[str, list[CompletedCandle]],
    benchmark_candles: list[CompletedCandle],
    grid: dict[str, list],
    *,
    validation_fraction: float = 0.35,
    maximum_combinations: int = 40,
    progress: Callable[[int, int, dict], None] | None = None,
) -> SweepResult:
    """Replay every combination, scored in sample and out of it.

    ``progress`` is called with (finished, total, parameters) before each
    combination. A sweep over months of candles takes hours on a small machine,
    and one that prints nothing until it is done is one an operator kills
    because they cannot tell it from a hang.
    """
    # Before anything is replayed: a sweep costs hours, and a grid that was
    # never going to produce a usable answer should say so in a second.
    validate_grid(grid)
    combos = build_combinations(grid, maximum_combinations)
    session_dates = sorted({candle.session_date for rows in candles_by_instrument.values() for candle in rows})
    in_sample_dates, validation_dates = _partition_dates(session_dates, validation_fraction)
    capital = Decimal(str(base_controls["account_capital"]))

    # When the grid only varies what a strategy carries, every combination
    # sees exactly the same candles and the same indicators -- so they can be
    # replayed together instead of once each. The replay already takes a list
    # of strategies and now computes each candle's indicators once for all of
    # them, so N variants in one pass cost about what one pass costs.
    #
    # A grid touching CONTROL_PARAMS or SETTINGS_PARAMS cannot share a pass:
    # those change the controls dict or the indicator periods, which the
    # replay takes once for the whole run. Those still go one at a time.
    #
    # EXIT_PARAMS are excluded too, though a strategy does carry them and they
    # could in principle share. One replay per variant costs minutes and
    # cannot be subtly wrong; sharing one would put the nested exit_rules
    # rebuild on a second code path, and a variant that quietly kept the base
    # rules would score as a difference that is not there.
    if combos and len(combos) > 1 and set(grid) <= STRATEGY_PARAMS:
        return _one_pass(
            base_strategy,
            base_controls,
            execution_controls,
            base_settings,
            candles_by_instrument,
            benchmark_candles,
            combos,
            in_sample_dates,
            validation_dates,
            capital,
            progress,
        )

    results: list[SweepCombination] = []
    for index, combo in enumerate(combos):
        if progress is not None:
            progress(index, len(combos), combo)
        strategy_variant = base_strategy.model_copy(
            update={key: value for key, value in combo.items() if key in STRATEGY_PARAMS}
        )
        # exit_rules is a model inside the model, so a flat update cannot reach
        # it: model_copy would attach the loose keys to the strategy, where
        # nothing reads them, and every variant would replay identically while
        # appearing to test something.
        exit_overrides = {key: value for key, value in combo.items() if key in EXIT_PARAMS}
        if exit_overrides:
            strategy_variant = strategy_variant.model_copy(
                update={"exit_rules": strategy_variant.exit_rules.model_copy(update=exit_overrides)}
            )
        controls_variant = {
            **base_controls,
            **{key: value for key, value in combo.items() if key in CONTROL_PARAMS},
        }
        settings_variant = _settings_namespace(base_settings, combo)
        result = run_completed_candle_backtest(
            candles_by_instrument,
            benchmark_candles,
            [strategy_variant],
            controls_variant,
            execution_controls,
            settings_variant,
        )
        in_trades = [t for t in result.trades if date.fromisoformat(t.session_date) in in_sample_dates]
        val_trades = [t for t in result.trades if date.fromisoformat(t.session_date) in validation_dates]
        in_metrics = _metrics(in_trades, capital)
        val_metrics = _metrics(val_trades, capital)
        proven = val_metrics["trades"] >= 3 and val_metrics["return_percent"] > 0
        results.append(SweepCombination(index, combo, in_metrics, val_metrics, proven))

    return SweepResult(combinations=tuple(results), best_index=_rank(results))
