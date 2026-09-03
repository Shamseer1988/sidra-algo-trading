"""Deterministic parameter-grid search for one strategy with an out-of-sample holdout.

Each grid combination is backtested once over the full range; trades are then split by
session date into an in-sample block and a later validation block. Combinations are
ranked by validation return so a configuration that only looks good in-sample ranks low.
Nothing is promoted automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import product
from types import SimpleNamespace

from app.services.backtesting import run_completed_candle_backtest
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
CONTROL_PARAMS = {"stop_atr_multiple", "min_stop_distance_percent", "risk_per_trade_percent"}
SETTINGS_PARAMS = {"opening_range_minutes", "ema_fast_period", "ema_slow_period", "atr_period"}
SWEEPABLE = STRATEGY_PARAMS | CONTROL_PARAMS | SETTINGS_PARAMS
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


def _normalise(value: float | int) -> float | int:
    """Integral values become ``int`` so they can index candle lists and EMA periods."""
    number = float(value)
    return int(number) if number.is_integer() else number


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
) -> SweepResult:
    combos = build_combinations(grid, maximum_combinations)
    session_dates = sorted({candle.session_date for rows in candles_by_instrument.values() for candle in rows})
    in_sample_dates, validation_dates = _partition_dates(session_dates, validation_fraction)
    capital = Decimal(str(base_controls["account_capital"]))

    results: list[SweepCombination] = []
    for index, combo in enumerate(combos):
        strategy_variant = base_strategy.model_copy(
            update={key: value for key, value in combo.items() if key in STRATEGY_PARAMS}
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

    ranked = sorted(
        results,
        key=lambda item: (
            item.validation["return_percent"],
            item.validation["net_pnl"],
            item.in_sample["return_percent"],
        ),
        reverse=True,
    )
    best = next((item.index for item in ranked if item.proven), ranked[0].index if ranked else None)
    return SweepResult(combinations=tuple(results), best_index=best)
