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
from app.services.exit_rules import ExitRules, time_exit_due, trail_to, under_account_deadline
from app.services.exit_rules import from_controls as exit_rules_from
from app.services.market_calculations import MARKET_TIMEZONE, CompletedCandle, indicator_snapshot
from app.services.paper_execution import (
    PaperExecutionControls,
    PaperOrderManager,
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
    # What the trade was sized to risk -- the live engine's daily allocation
    # adds these up. Defaulted so a caller building results by hand still works.
    risk_amount: Decimal = Decimal("0")
    # One strategy on one share on one day is one chain. In ``all_signals``
    # mode a chain can hold several candidates, in time order; the portfolio
    # simulator takes at most one of them, as live does (see
    # ``run_completed_candle_backtest``).
    chain_key: str = ""
    sequence: int = 0


@dataclass(frozen=True)
class BacktestResult:
    trades: tuple[BacktestTradeResult, ...]
    summary: dict
    data_fingerprint: str
    source_candle_count: int


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


@dataclass(frozen=True)
class _StopOrder:
    """The two attributes ``PaperOrderManager._stop_fill`` reads from a paper order."""

    side: str
    stop_price: Decimal


DAILY_TIMEFRAME_SECONDS = 86_400


def _session_daily_candle(session_rows: list[CompletedCandle]) -> CompletedCandle:
    """Collapse one session's completed candles into a single daily OHLCV candle."""
    ordered = sorted(session_rows, key=lambda candle: candle.opened_at)
    return CompletedCandle(
        instrument_token=ordered[0].instrument_token,
        timeframe_seconds=DAILY_TIMEFRAME_SECONDS,
        opened_at=ordered[0].opened_at,
        closed_at=ordered[-1].closed_at,
        open=ordered[0].open,
        high=max(candle.high for candle in ordered),
        low=min(candle.low for candle in ordered),
        close=ordered[-1].close,
        volume=sum(candle.volume for candle in ordered),
        tick_count=sum(candle.tick_count for candle in ordered),
    )


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
    *,
    entry_price: Decimal,
    rules: ExitRules,
) -> tuple[Decimal, datetime, str]:
    """Exit after the entry bar closes; simultaneous touches choose the protective stop.

    The stop moves if the strategy's rules say it should. Until this existed the
    backtest held one fixed stop for the whole trade, so a strategy with
    break-even trailing switched on was measured as a strategy without it --
    and a sweep over trailing values would have scored every variant
    identically while appearing to test something.

    ``trail_to`` is the paper and live engines' own function, called here with
    the same arguments they pass: the entry *fill* for ``entry``, and
    ``risk_per_unit`` from the signal's own entry and stop rather than from the
    stop as it currently stands -- once the stop has moved, the distance to it
    is no longer the R that "one R ahead" refers to.

    The trail is applied **after** the candle's own hit check and from that
    candle's close and extreme, which is what the paper engine does: it runs on
    completed candles, so a level derived from a candle cannot also protect
    that same candle. Inside one bar there is no way to know whether the
    extreme came before or after the close, and assuming the favourable order
    is how a backtest invents money.

    ``atr`` is None on purpose. It is used only by ATR_TRAIL, which the live
    path refuses to apply (``live_exit_manager``: moving a stop means cancel
    and replace, so a rule that moves most minutes would leave the position
    unprotected again and again). Passing None makes an ATR_TRAIL strategy
    behave here the way it behaves live -- no trail -- rather than scoring a
    backtest on an exit the account would never get.
    """
    side = decision.side
    assert side and decision.stop_price and decision.target_price
    stop = _decimal(decision.stop_price)
    target = _decimal(decision.target_price)
    # From the signal, like the paper engine, and fixed for the trade.
    risk_per_unit = abs(_decimal(decision.entry_price) - _decimal(decision.stop_price))
    trailed = False
    opened_at = candles[entry_index].opened_at
    after = candles[entry_index + 1 :]
    band = float(getattr(execution_controls, "stop_limit_band_percent", 0.0) or 0.0)
    stop_order_side = exit_side(side)
    for position, candle in enumerate(after):
        # Where the stop fills, by the paper engine's own rule
        # (``PaperOrderManager._stop_fill``): at the stop when price trades
        # through it, at the open when the candle opens beyond it, and -- past
        # Upstox's SL-limit band -- only if price comes back to the band's
        # edge. A gap through the stop used to be booked at the stop, a price
        # that never traded, which flattered every strategy that holds through
        # a gap. Not filled means the order rests and the next candle is tried.
        stop_order = _StopOrder(side=stop_order_side, stop_price=stop)
        stop_filled, stop_fill = PaperOrderManager._stop_fill(stop_order, candle, band)
        target_hit = candle.high >= target if side == "LONG" else candle.low <= target
        if stop_filled:
            reason = "TRAIL_STOP" if trailed else "STOP"
            return slipped_price(stop_fill, exit_side(side), execution_controls.slippage_bps), candle.closed_at, reason
        if target_hit:
            return slipped_price(target, exit_side(side), execution_controls.slippage_bps), candle.closed_at, "TARGET"
        # The clock, the way paper applies it: due at this candle's close,
        # filled at the next candle's open. Without it a strategy that squares
        # off at 3 PM was measured holding to the last candle of the day.
        if time_exit_due(opened_at=opened_at, now=candle.closed_at, rules=rules) is not None:
            following = after[position + 1] if position + 1 < len(after) else None
            reference = following.open if following is not None else candle.close
            exited_at = following.opened_at if following is not None else candle.closed_at
            return slipped_price(reference, exit_side(side), execution_controls.slippage_bps), exited_at, "TIME"
        moved = trail_to(
            side=side,
            entry=entry_price,
            current_stop=stop,
            risk_per_unit=risk_per_unit,
            candle_close=candle.close,
            candle_extreme=candle.high if side == "LONG" else candle.low,
            atr=None,
            rules=rules,
        )
        if moved is not None:
            stop, trailed = moved, True
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


def _minute_volumes(by_date: dict) -> dict:
    """Each session's volume per IST minute, computed once per instrument."""
    return {
        day: {candle.opened_at.astimezone(MARKET_TIMEZONE).strftime("%H:%M"): candle.volume for candle in rows}
        for day, rows in by_date.items()
    }


def _time_of_day_baseline(minute_volumes: dict, session_date, sessions: int) -> dict[str, float]:
    """Average volume per IST minute over the prior ``sessions`` sessions in the replay.

    The live aggregator's ``_time_of_day_baseline``, from the candles the replay
    already holds: only sessions before this one, so nothing from the day being
    replayed reaches its own baseline. The first sessions of a range have less
    history than live does and get a thinner baseline, or none.
    """
    prior = [day for day in sorted(minute_volumes) if day < session_date][-sessions:]
    buckets: dict[str, list[int]] = defaultdict(list)
    for day in prior:
        for minute, volume in minute_volumes[day].items():
            buckets[minute].append(volume)
    return {key: round(sum(values) / len(values), 4) for key, values in buckets.items() if values}


def run_completed_candle_backtest(
    candles_by_instrument: dict[str, list[CompletedCandle]],
    benchmark_candles: list[CompletedCandle],
    strategies: list[StrategyConfiguration],
    controls: dict,
    execution_controls: PaperExecutionControls,
    settings: Settings,
    *,
    all_signals: bool = False,
) -> BacktestResult:
    """Replay immutable candles with explicit next-candle fills and no future indicators.

    Each strategy is replayed on each share independently. By default a
    strategy stops on a share after its first signal of the session, which is
    what live does when that signal is taken.

    ``all_signals=True`` is for ``portfolio_simulation``. Live does not always
    take a signal: a daily ceiling or a risk gate can refuse it, and then the
    scanner sets the state back to AWAITING and the setup may form again later
    that session (``scanner_orchestration._evaluate_strategy``). So in this mode
    every signal is emitted as a candidate and the state machine is reset
    exactly as a refusal would reset it. Candidates of one strategy on one share
    on one day share a ``chain_key`` and are numbered by ``sequence``; the
    portfolio pass takes at most one per chain and never a later one after it.

    Exits use the strategy's own rules capped at the account's square-off
    (``session_square_off_time``), the way ``live_exit_manager`` caps them.
    """
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
        daily_by_date = {day: _session_daily_candle(day_rows) for day, day_rows in by_date.items() if day_rows}
        minute_volumes = _minute_volumes(by_date)
        for session_date, session_rows in sorted(by_date.items()):
            benchmark = benchmark_by_date.get(session_date, [])
            if len(session_rows) < 3 or not benchmark:
                continue
            prior_daily = [daily for day, daily in sorted(daily_by_date.items()) if day < session_date][
                -settings.daily_history_sessions :
            ]
            # ``getattr`` because a sweep or test may hand in a slimmer settings
            # object; 10 is the Settings default.
            time_of_day_baseline = _time_of_day_baseline(
                minute_volumes, session_date, getattr(settings, "rvol_baseline_sessions", 10)
            )
            # One indicator snapshot per candle, shared by every strategy.
            #
            # It was computed inside the strategy loop, though it depends on
            # the candles and the settings and on nothing a strategy carries.
            # With one strategy that cost nothing; with N it did the same work
            # N times, and a parameter sweep is exactly N variants of one
            # strategy. The benchmark snapshot was worse: identical for every
            # instrument on a session, and recomputed for each of them.
            #
            # The loops are therefore inverted -- candles outside, strategies
            # inside -- with the per-strategy state that used to be a local
            # local held in a dict. ``_analytics`` sorts trades itself, so the
            # order they are appended in does not reach the result.
            pending = {strategy.id: strategy for strategy in sorted(strategies, key=lambda item: item.id)}
            states = dict.fromkeys(pending, AWAITING)
            controls_for = {key: value.effective_controls(controls) for key, value in pending.items()}
            # The account deadline caps each strategy's own square-off, as live
            # does. Without it a strategy that says nothing about the clock was
            # held to the last candle of the day, past the 3 PM the account is
            # flat by.
            rules_for = {
                key: under_account_deadline(exit_rules_from(controls_for[key]), controls.get("session_square_off_time"))
                for key in pending
            }
            sequence_for = dict.fromkeys(pending, 0)
            # Keyed by the candle's close, which is what decides the slice. One
            # session's worth at a time, so this is bounded by a few hundred
            # entries however long the range is.
            nifty_cache: dict[object, dict] = {}
            cursor = 0
            for index in range(len(session_rows) - 2):
                if not pending:
                    break
                candle = session_rows[index]
                history = session_rows[: index + 1]
                # The benchmark prefix by a walking cursor rather than a filter
                # over the whole session per candle. Both series are in time
                # order, so the cursor only ever moves forward.
                while cursor < len(benchmark) and benchmark[cursor].closed_at <= candle.closed_at:
                    cursor += 1
                benchmark_history = benchmark[:cursor]
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
                    atr_period=settings.atr_period,
                    daily_candles=prior_daily,
                    time_of_day_volume_baseline=time_of_day_baseline,
                )
                nifty = nifty_cache.get(candle.closed_at)
                if nifty is None:
                    nifty = indicator_snapshot(
                        benchmark_history,
                        benchmark_history,
                        opening_range_minutes=settings.opening_range_minutes,
                        fast_ema_period=settings.ema_fast_period,
                        slow_ema_period=settings.ema_slow_period,
                        volume_lookback=settings.volume_lookback_candles,
                        is_nifty=True,
                        atr_period=settings.atr_period,
                    )
                    nifty_cache[candle.closed_at] = nifty

                signalled: list[str] = []
                for key, strategy in pending.items():
                    decision = StrategyRegistry.evaluate(
                        strategy, candle, indicators, nifty, controls_for[key], states[key]
                    )
                    states[key] = decision.next_state
                    if decision.next_state != SIGNALLED or decision.side is None:
                        continue
                    if all_signals:
                        # As if refused: the scanner records AWAITING and the
                        # setup must form again. Whether it was in fact taken
                        # is the portfolio pass's decision, not this one's.
                        states[key] = AWAITING
                    else:
                        signalled.append(key)
                    sequence = sequence_for[key]
                    sequence_for[key] += 1
                    entry_candle = session_rows[index + 1]
                    entry_price = slipped_price(
                        entry_candle.open, entry_side(decision.side), execution_controls.slippage_bps
                    )
                    exit_price, exited_at, exit_reason = _exit_trade(
                        decision,
                        session_rows,
                        index + 1,
                        execution_controls,
                        entry_price=entry_price,
                        # The strategy's own rules, read the way paper reads
                        # them, so a per-strategy override reaches the exit --
                        # capped at the account deadline.
                        rules=rules_for[key],
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
                            risk_amount=_money(risk),
                            chain_key=f"{strategy.id}:{session_date.isoformat()}:{instrument}",
                            sequence=sequence,
                        )
                    )
                # A strategy takes one trade per instrument per session, which
                # the old loop expressed as ``break``. With the loops inverted
                # that becomes dropping it from the pending set, and the candle
                # loop ends when the last one is gone.
                for key in signalled:
                    del pending[key]
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
