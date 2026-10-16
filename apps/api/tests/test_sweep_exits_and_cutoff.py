"""The sweep can test how a trade leaves and when it may be entered.

Two parameters were untestable, and both are the kind the live results point
at. Trailing lived inside ``exit_rules``, one level below the flat update every
other strategy parameter uses, so adding its name to the sweep alone would have
replayed every variant identically while appearing to test something. The entry
cutoff was already obeyed by every strategy, but ``--param`` parsed each value
with float(), so ``13:30`` could not even be typed.

Underneath both sat a larger gap: the backtest held one fixed stop for the whole
trade. A strategy with break-even trailing switched on was measured as a
strategy without it.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.backtest_sweep import (
    EXIT_PARAMS,
    LIVE_TRAILING_RULES,
    SWEEPABLE,
    build_combinations,
    validate_grid,
)
from app.services.backtesting import _exit_trade, run_completed_candle_backtest
from app.services.exit_rules import BREAKEVEN_AT_R, NO_TRAIL, ExitRules
from app.services.market_calculations import CompletedCandle
from app.services.paper_execution import PaperExecutionControls
from app.services.strategy_registry import StrategyConfiguration
from app.services.trading_calendar import MARKET_TIMEZONE

# 09:15 IST, the real market open. The session has to start here: the opening
# range is anchored to MARKET_OPEN rather than to the first candle supplied, so
# a series beginning in the afternoon has no range to break and can never
# signal -- which is exactly what the control test below exists to catch.
OPEN = datetime(2026, 1, 5, 3, 45, tzinfo=UTC)
# The breakout is padded out to here: 09:15 + 265 minutes is 13:40 IST, after a
# 13:30 cutoff and before a 14:45 one, so one series answers both.
BREAKOUT = 265


def candle(
    index: int, *, instrument: str, open_price: str, high: str, low: str, close: str, start: datetime = OPEN
) -> CompletedCandle:
    opened_at = start + timedelta(minutes=index)
    return CompletedCandle(
        instrument_token=instrument,
        timeframe_seconds=60,
        opened_at=opened_at,
        closed_at=opened_at + timedelta(minutes=1),
        open=Decimal(open_price),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=1_000,
        tick_count=10,
    )


def series(instrument: str) -> list[CompletedCandle]:
    """A quiet morning, then an opening-range break and a retest after 13:30.

    The four-candle break-and-retest shape is borrowed from test_backtesting,
    which already proves it produces a trade. Only the timing changes: the flat
    stretch is padded so the signal lands in the afternoon, which is the only
    way one series can sit on either side of a cutoff.
    """
    rows = [
        candle(index, instrument=instrument, open_price="100", high="101", low="99", close="100")
        for index in range(BREAKOUT)
    ]
    rows.extend(
        [
            candle(BREAKOUT, instrument=instrument, open_price="100", high="102.5", low="101.5", close="102"),
            candle(BREAKOUT + 1, instrument=instrument, open_price="102", high="102.5", low="101.1", close="102"),
            candle(BREAKOUT + 2, instrument=instrument, open_price="102", high="102.5", low="101.5", close="102.3"),
            candle(BREAKOUT + 3, instrument=instrument, open_price="102.3", high="104", low="101.5", close="103.5"),
        ]
    )
    return rows


def settings() -> SimpleNamespace:
    return SimpleNamespace(
        opening_range_minutes=15,
        ema_fast_period=9,
        ema_slow_period=21,
        volume_lookback_candles=20,
        atr_period=14,
        daily_history_sessions=40,
    )


def controls(**overrides) -> dict:
    return {
        "account_capital": 100_000,
        "risk_per_trade_percent": 0.5,
        "minimum_score": 0,
        "minimum_rr": 1,
        "volume_multiplier": 0.5,
        "retest_tolerance_percent": 0.15,
        "minimum_ema_spread_percent": 0,
        "trade_start_time": "09:15",
        "trade_cutoff_time": "15:00",
        **overrides,
    }


def strategy() -> StrategyConfiguration:
    return StrategyConfiguration(
        id="orb-cutoff-test",
        name="ORB Cutoff Test",
        universe=["NSE:TEST"],
        minimum_score=0,
        minimum_rr=1,
        volume_multiplier=0.5,
        minimum_ema_spread_percent=0,
    )


def free_execution() -> PaperExecutionControls:
    """No costs and no slippage, so a price in a test is the price asserted."""
    return PaperExecutionControls(
        slippage_bps=0,
        brokerage_percent=0,
        stt_sell_percent=0,
        exchange_transaction_percent=0,
        gst_percent=0,
        sebi_percent=0,
        stamp_duty_buy_percent=0,
    )


def backtest(cutoff: str):
    return run_completed_candle_backtest(
        {"NSE:TEST": series("NSE:TEST")},
        series("NSE:26000"),
        [strategy()],
        controls(trade_cutoff_time=cutoff),
        free_execution(),
        settings(),
    )


# --- the entry cutoff -----------------------------------------------------


def test_a_late_cutoff_takes_the_afternoon_trade() -> None:
    """The control. Without it the cutoff test below would pass on a day that
    produced no trade for reasons of its own, and prove nothing."""
    result = backtest("14:45")
    assert len(result.trades) == 1
    entered = result.trades[0].entered_at.astimezone(MARKET_TIMEZONE)
    assert entered.strftime("%H:%M") > "13:30"


def test_a_cutoff_before_the_signal_takes_no_trade() -> None:
    """Same candles, same strategy, only the cutoff differs."""
    assert backtest("13:30").trades == ()


@pytest.mark.parametrize("cutoff", ["13:30", "14:45"])
def test_the_cutoff_is_read_from_the_controls_the_sweep_varies(cutoff: str) -> None:
    """trade_cutoff_time is a control, so a sweep over it reaches the strategy
    through effective_controls without any new plumbing."""
    assert "trade_cutoff_time" in SWEEPABLE
    result = backtest(cutoff)
    assert len(result.trades) == (0 if cutoff == "13:30" else 1)


# --- the trail, applied by the backtest -----------------------------------


def decision(*, entry: str = "100", stop: str = "99", target: str = "102"):
    return SimpleNamespace(
        side="LONG",
        entry_price=Decimal(entry),
        stop_price=Decimal(stop),
        target_price=Decimal(target),
        quantity=10,
        risk_amount=Decimal("10"),
    )


def after_entry() -> list[CompletedCandle]:
    """Entry bar, then one that gains 1R, then two that give it back.

    With no trail the stop at 99 is hit on the last bar. With break-even at 1R
    the stop moves to the entry after the second bar and is hit on the third,
    so the two rules exit at different prices on identical candles.
    """
    return [
        candle(0, instrument="NSE:TEST", open_price="100", high="100.2", low="99.8", close="100"),
        candle(1, instrument="NSE:TEST", open_price="100", high="101", low="100.0", close="100.8"),
        candle(2, instrument="NSE:TEST", open_price="100.8", high="100.9", low="99.9", close="100.0"),
        candle(3, instrument="NSE:TEST", open_price="100", high="100.1", low="98.5", close="98.6"),
    ]


def exit_with(rules: ExitRules):
    return _exit_trade(
        decision(),
        after_entry(),
        0,
        free_execution(),
        entry_price=Decimal("100"),
        rules=rules,
    )


def test_without_a_trail_the_original_stop_is_what_gets_hit() -> None:
    price, _, reason = exit_with(ExitRules(trailing_rule=NO_TRAIL))
    assert (price, reason) == (Decimal("99"), "STOP")


def test_break_even_moves_the_stop_to_the_entry_after_one_r() -> None:
    """The whole point: identical candles, a different exit."""
    price, _, reason = exit_with(ExitRules(trailing_rule=BREAKEVEN_AT_R, trailing_trigger_r=1.0))
    assert (price, reason) == (Decimal("100"), "TRAIL_STOP")


def test_a_trigger_the_trade_never_reaches_leaves_the_stop_alone() -> None:
    """It gained 1R, not 2R, so a 2R trigger must not move anything."""
    price, _, reason = exit_with(ExitRules(trailing_rule=BREAKEVEN_AT_R, trailing_trigger_r=2.0))
    assert (price, reason) == (Decimal("99"), "STOP")


def test_the_trail_cannot_protect_the_candle_it_was_derived_from() -> None:
    """A level computed from a candle's own high cannot also have guarded that
    candle's low: inside one bar there is no way to know which came first, and
    assuming the favourable order is how a backtest invents money.

    Here the 1R high and the drop below the entry are the same bar. The stop
    must still be at 99 while that bar is judged.
    """
    rows = [
        candle(0, instrument="NSE:TEST", open_price="100", high="100.2", low="99.8", close="100"),
        candle(1, instrument="NSE:TEST", open_price="100", high="101", low="99.5", close="100.5"),
        candle(2, instrument="NSE:TEST", open_price="100.5", high="100.6", low="98.5", close="98.6"),
    ]
    price, _, reason = _exit_trade(
        decision(),
        rows,
        0,
        free_execution(),
        entry_price=Decimal("100"),
        rules=ExitRules(trailing_rule=BREAKEVEN_AT_R, trailing_trigger_r=1.0),
    )
    # Bar 1 dipped to 99.5, above the 99 stop, so it survived; the trail then
    # moved the stop to 100 and bar 2 took it.
    assert (price, reason) == (Decimal("100"), "TRAIL_STOP")


# --- what the grid accepts ------------------------------------------------


def test_the_grid_the_phase_asked_for_builds_four_combinations() -> None:
    grid = {"trailing_rule": [NO_TRAIL, BREAKEVEN_AT_R], "trade_cutoff_time": ["13:30", "14:45"]}
    validate_grid(grid)
    combos = build_combinations(grid, 40)
    assert len(combos) == 4
    assert {(combo["trailing_rule"], combo["trade_cutoff_time"]) for combo in combos} == {
        (NO_TRAIL, "13:30"),
        (NO_TRAIL, "14:45"),
        (BREAKEVEN_AT_R, "13:30"),
        (BREAKEVEN_AT_R, "14:45"),
    }


def test_atr_trail_is_refused_with_the_reason() -> None:
    """It is never applied live, so a backtest of it scores an exit the account
    would not get. Refused before the hours of replay, not after."""
    with pytest.raises(ValueError) as refusal:
        validate_grid({"trailing_rule": ["ATR_TRAIL"]})
    assert "never applies it" in str(refusal.value)
    assert "BREAKEVEN_AT_R" in str(refusal.value)


def test_a_trailing_rule_that_is_not_a_rule_is_refused() -> None:
    with pytest.raises(ValueError):
        validate_grid({"trailing_rule": ["BREAKEVEN"]})


@pytest.mark.parametrize("bad", ["1330", "13.30", "25:00", "13:60", "afternoon"])
def test_a_cutoff_that_is_not_hh_mm_is_refused(bad: str) -> None:
    """It is compared as text against each candle's IST time, so a different
    spelling would compare wrongly instead of failing."""
    with pytest.raises(ValueError) as refusal:
        validate_grid({"trade_cutoff_time": [bad]})
    assert "HH:MM" in str(refusal.value)


def test_the_exit_parameters_are_sweepable_and_kept_apart() -> None:
    """They are nested in exit_rules, so they cannot ride the flat update."""
    assert EXIT_PARAMS <= SWEEPABLE
    assert {"trailing_rule", "trailing_trigger_r"} == EXIT_PARAMS
    assert "trailing_atr_multiple" not in SWEEPABLE
    assert LIVE_TRAILING_RULES == (NO_TRAIL, BREAKEVEN_AT_R)
