"""Deterministic historical research using only completed market candles.

This module is intentionally isolated from brokers, Redis, and wall-clock state. A
strategy can observe a candle only after it has closed; any resulting simulated entry
is filled from the following candle, so future bars cannot influence a decision.
"""

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256

from app.core.config import Settings
from app.services.market_calculations import CompletedCandle, indicator_snapshot
from app.services.paper_execution import (
    PaperExecutionControls,
    _money,
    entry_side,
    exit_side,
    slipped_price,
    transaction_costs,
)
from app.services.paper_strategy import AWAITING, SIGNALLED
from app.services.strategy_registry import StrategyConfiguration, StrategyRegistry


@dataclass(frozen=True)
class BacktestTradeResult:
    trade_key: str
    strategy_id: str
    strategy_name: str
    strategy_version: int
    instrument_token: str
    session_date: str
    side: str
    quantity: int
    signal_at: datetime
    entered_at: datetime
    exited_at: datetime
    entry_price: Decimal
    exit_price: Decimal
    gross_pnl: Decimal
    fees_total: Decimal
    net_pnl: Decimal
    realized_r: Decimal
    exit_reason: str


@dataclass(frozen=True)
class BacktestResult:
    trades: tuple[BacktestTradeResult, ...]
    summary: dict
    data_fingerprint: str
    source_candle_count: int


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def candle_fingerprint(
    candles_by_instrument: dict[str, list[CompletedCandle]],
    strategies: list[StrategyConfiguration],
    controls: dict,
    execution_controls: PaperExecutionControls,
) -> str:
    payload = {
        "candles": {
            instrument: [
                {
                    "opened_at": candle.opened_at.isoformat(),
                    "closed_at": candle.closed_at.isoformat(),
                    "open": str(candle.open),
                    "high": str(candle.high),
                    "low": str(candle.low),
                    "close": str(candle.close),
                    "volume": candle.volume,
                    "ticks": candle.tick_count,
                }
                for candle in sorted(rows, key=lambda row: row.opened_at)
            ]
            for instrument, rows in sorted(candles_by_instrument.items())
        },
        "strategies": [item.model_dump(mode="json") for item in sorted(strategies, key=lambda item: item.id)],
        "controls": controls,
        "execution": execution_controls.model_dump(mode="json"),
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _exit_trade(
    decision,
    candles: list[CompletedCandle],
    entry_index: int,
    execution_controls: PaperExecutionControls,
) -> tuple[Decimal, datetime, str]:
    """Exit after the entry bar closes; simultaneous touches choose the protective stop."""
    side = decision.side
    assert side and decision.stop_price and decision.target_price
    for candle in candles[entry_index + 1 :]:
        stop_hit = candle.low <= decision.stop_price if side == "LONG" else candle.high >= decision.stop_price
        target_hit = candle.high >= decision.target_price if side == "LONG" else candle.low <= decision.target_price
        if stop_hit or target_hit:
            reason = "STOP" if stop_hit else "TARGET"
            reference = decision.stop_price if stop_hit else decision.target_price
            return slipped_price(reference, exit_side(side), execution_controls.slippage_bps), candle.closed_at, reason
    final_candle = candles[-1]
    return (
        slipped_price(final_candle.close, exit_side(side), execution_controls.slippage_bps),
        final_candle.closed_at,
        "END_OF_DATA",
    )


def _analytics(trades: list[BacktestTradeResult], initial_capital: Decimal) -> dict:
    ordered = sorted(trades, key=lambda trade: (trade.exited_at, trade.trade_key))
    equity = initial_capital
    peak = initial_capital
    max_drawdown = Decimal("0")
    gross_profit = Decimal("0")
    gross_loss = Decimal("0")
    equity_curve = [{"at": None, "equity": float(equity), "drawdown": 0.0}]
    by_strategy: dict[str, list[BacktestTradeResult]] = defaultdict(list)
    for trade in ordered:
        equity += trade.net_pnl
        peak = max(peak, equity)
        drawdown = peak - equity
        max_drawdown = max(max_drawdown, drawdown)
        if trade.net_pnl > 0:
            gross_profit += trade.net_pnl
        elif trade.net_pnl < 0:
            gross_loss += abs(trade.net_pnl)
        by_strategy[f"{trade.strategy_id}:v{trade.strategy_version}"].append(trade)
        equity_curve.append(
            {"at": trade.exited_at.isoformat(), "equity": float(_money(equity)), "drawdown": float(_money(drawdown))}
        )

    def metrics(items: list[BacktestTradeResult]) -> dict:
        net = sum((item.net_pnl for item in items), start=Decimal("0"))
        winners = sum(item.net_pnl > 0 for item in items)
        losses = sum(item.net_pnl < 0 for item in items)
        profit = sum((item.net_pnl for item in items if item.net_pnl > 0), start=Decimal("0"))
        loss = sum((-item.net_pnl for item in items if item.net_pnl < 0), start=Decimal("0"))
        return {
            "trades": len(items),
            "winners": winners,
            "losers": losses,
            "win_rate": round((winners / len(items) * 100) if items else 0, 2),
            "net_pnl": float(_money(net)),
            "profit_factor": round(float(profit / loss), 4) if loss else None,
        }

    return {
        **metrics(ordered),
        "initial_capital": float(_money(initial_capital)),
        "final_equity": float(_money(equity)),
        "return_percent": round(float((equity - initial_capital) * Decimal("100") / initial_capital), 4)
        if initial_capital
        else 0,
        "max_drawdown": float(_money(max_drawdown)),
        "equity_curve": equity_curve,
        "strategy_comparison": [
            {
                "strategy_id": items[0].strategy_id,
                "strategy_name": items[0].strategy_name,
                "strategy_version": items[0].strategy_version,
                **metrics(items),
            }
            for _, items in sorted(by_strategy.items())
        ],
    }


def run_completed_candle_backtest(
    candles_by_instrument: dict[str, list[CompletedCandle]],
    benchmark_candles: list[CompletedCandle],
    strategies: list[StrategyConfiguration],
    controls: dict,
    execution_controls: PaperExecutionControls,
    settings: Settings,
) -> BacktestResult:
    """Replay immutable candles with explicit next-candle fills and no future indicators."""
    normalized = {
        key: sorted(value, key=lambda candle: candle.opened_at) for key, value in candles_by_instrument.items()
    }
    benchmark_by_date: dict[object, list[CompletedCandle]] = defaultdict(list)
    for candle in sorted(benchmark_candles, key=lambda item: item.opened_at):
        benchmark_by_date[candle.session_date].append(candle)
    trades: list[BacktestTradeResult] = []
    for instrument, rows in sorted(normalized.items()):
        by_date: dict[object, list[CompletedCandle]] = defaultdict(list)
        for candle in rows:
            by_date[candle.session_date].append(candle)
        for session_date, session_rows in sorted(by_date.items()):
            benchmark = benchmark_by_date.get(session_date, [])
            if len(session_rows) < 3 or not benchmark:
                continue
            for strategy in sorted(strategies, key=lambda item: item.id):
                state = AWAITING
                effective_controls = strategy.effective_controls(controls)
                for index in range(len(session_rows) - 2):
                    candle = session_rows[index]
                    history = session_rows[: index + 1]
                    benchmark_history = [item for item in benchmark if item.closed_at <= candle.closed_at]
                    if not benchmark_history:
                        continue
                    indicators = indicator_snapshot(
                        history,
                        benchmark_history,
                        opening_range_minutes=settings.opening_range_minutes,
                        fast_ema_period=settings.ema_fast_period,
                        slow_ema_period=settings.ema_slow_period,
                        volume_lookback=settings.volume_lookback_candles,
                        is_nifty=False,
                    )
                    nifty = indicator_snapshot(
                        benchmark_history,
                        benchmark_history,
                        opening_range_minutes=settings.opening_range_minutes,
                        fast_ema_period=settings.ema_fast_period,
                        slow_ema_period=settings.ema_slow_period,
                        volume_lookback=settings.volume_lookback_candles,
                        is_nifty=True,
                    )
                    decision = StrategyRegistry.evaluate(strategy, candle, indicators, nifty, effective_controls, state)
                    state = decision.next_state
                    if decision.next_state != SIGNALLED or decision.side is None:
                        continue
                    entry_candle = session_rows[index + 1]
                    entry_price = slipped_price(
                        entry_candle.open, entry_side(decision.side), execution_controls.slippage_bps
                    )
                    exit_price, exited_at, exit_reason = _exit_trade(
                        decision, session_rows, index + 1, execution_controls
                    )
                    entry_costs = transaction_costs(
                        entry_price, decision.quantity, entry_side(decision.side), execution_controls
                    )
                    exit_costs = transaction_costs(
                        exit_price, decision.quantity, exit_side(decision.side), execution_controls
                    )
                    gross = (exit_price - entry_price) * decision.quantity
                    if decision.side == "SHORT":
                        gross = -gross
                    fees = entry_costs.total + exit_costs.total
                    net = _money(gross - fees)
                    risk = _decimal(decision.risk_amount or 0)
                    trades.append(
                        BacktestTradeResult(
                            trade_key=f"{strategy.id}:v{strategy.version}:{session_date.isoformat()}:{instrument}:{candle.opened_at.isoformat()}",
                            strategy_id=strategy.id,
                            strategy_name=strategy.name,
                            strategy_version=strategy.version,
                            instrument_token=instrument,
                            session_date=session_date.isoformat(),
                            side=decision.side,
                            quantity=decision.quantity,
                            signal_at=candle.closed_at,
                            entered_at=entry_candle.opened_at,
                            exited_at=exited_at,
                            entry_price=entry_price,
                            exit_price=exit_price,
                            gross_pnl=_money(gross),
                            fees_total=_money(fees),
                            net_pnl=net,
                            realized_r=_money(net / risk) if risk else Decimal("0"),
                            exit_reason=exit_reason,
                        )
                    )
                    break
    initial_capital = _decimal(controls["account_capital"])
    return BacktestResult(
        trades=tuple(sorted(trades, key=lambda trade: (trade.exited_at, trade.trade_key))),
        summary=_analytics(trades, initial_capital),
        data_fingerprint=candle_fingerprint(
            {**normalized, "__benchmark__": sorted(benchmark_candles, key=lambda candle: candle.opened_at)},
            strategies,
            controls,
            execution_controls,
        ),
        source_candle_count=sum(len(rows) for rows in normalized.values()) + len(benchmark_candles),
    )
