"""A sweep over what a strategy carries replays the candles once, not N times.

Every combination of a ``minimum_score`` or ``minimum_rr`` grid sees the same
candles and the same indicators, so replaying them separately recomputed every
indicator snapshot once per combination. On the live watchlist roughly one
signal a day arrives across 27 instruments, which means nearly every
instrument-session is scanned end to end by every variant -- the case where
that waste is worst. Measured on sessions where nothing signals, four
combinations took 20.4s separately and 5.1s in one pass, exactly 4.00x, with
identical results.

The risk in sharing a pass is that one variant's decisions reach another, so
the equivalence is asserted here rather than assumed: the same grid is run
down both paths and the metrics must match. The slow path is forced by adding
a single-valued control parameter, which cannot share a pass because controls
are taken once for the whole replay -- same work, same candles, different
route through the sweep.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest

from app.api.routes.settings import DEFAULT_TRADING_CONTROLS
from app.core.config import get_settings
from app.services.backtest_sweep import run_parameter_sweep
from app.services.backtesting import run_completed_candle_backtest
from app.services.market_calculations import CompletedCandle
from app.services.paper_execution import DEFAULT_PAPER_EXECUTION_CONTROLS, PaperExecutionControls
from app.services.strategy_registry import DEFAULT_STRATEGIES, StrategyConfiguration
from app.services.trading_calendar import MARKET_TIMEZONE

SESSIONS = [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)]
TOKEN = "NSE_EQ|SHARED-A"
BENCH = "NSE_INDEX|Nifty 50"


def _candle(token: str, at: datetime, price: float, volume: int, low: float | None = None) -> CompletedCandle:
    return CompletedCandle(
        instrument_token=token,
        timeframe_seconds=60,
        opened_at=at,
        closed_at=at + timedelta(minutes=1),
        open=Decimal(f"{price:.2f}"),
        high=Decimal(f"{price * 1.0006:.2f}"),
        low=Decimal(f"{(low if low is not None else price * 0.9994):.2f}"),
        close=Decimal(f"{price:.2f}"),
        volume=volume,
        tick_count=1,
    )


def _session(token: str, day: date, base: float) -> list[CompletedCandle]:
    """An opening range, a breakout on volume, a retest, then a run.

    A random walk never forms one of these, so a fixture built from one tests
    an engine that finds no trades at all.
    """
    rows = []
    start = datetime.combine(day, time(9, 15), tzinfo=MARKET_TIMEZONE)
    level = base * 1.004
    for index in range(15):
        price = base + (level - base) * (index / 20)
        rows.append(_candle(token, start + timedelta(minutes=index), price, 9_000))
    price = level
    for index in range(15, 25):
        price *= 1.0012
        rows.append(_candle(token, start + timedelta(minutes=index), price, 42_000))
    peak = price
    for index in range(25, 34):
        price = peak - (peak - level) * ((index - 24) / 9)
        rows.append(_candle(token, start + timedelta(minutes=index), price, 26_000, low=level * 1.0002))
    rows.append(_candle(token, start + timedelta(minutes=34), level * 1.0035, 48_000, low=level * 1.0001))
    price = level * 1.0035
    for index in range(35, 120):
        price *= 1.00035
        rows.append(_candle(token, start + timedelta(minutes=index), price, 15_000))
    return rows


@pytest.fixture(scope="module")
def replay_inputs():
    instrument = [candle for day in SESSIONS for candle in _session(TOKEN, day, 480.0)]
    benchmark = [candle for day in SESSIONS for candle in _session(BENCH, day, 24_000.0)]
    return {TOKEN: instrument}, benchmark


def _base() -> StrategyConfiguration:
    return StrategyConfiguration.model_validate({**DEFAULT_STRATEGIES[0], "minimum_score": 50})


def _sweep(replay_inputs, grid: dict):
    candles, benchmark = replay_inputs
    return run_parameter_sweep(
        _base(),
        dict(DEFAULT_TRADING_CONTROLS),
        PaperExecutionControls.model_validate(DEFAULT_PAPER_EXECUTION_CONTROLS),
        get_settings(),
        candles,
        benchmark,
        grid,
    )


def test_the_fixture_actually_produces_trades(replay_inputs) -> None:
    """Guards every assertion below. An equivalence test over zero trades
    passes while proving nothing."""
    candles, benchmark = replay_inputs
    result = run_completed_candle_backtest(
        candles,
        benchmark,
        [_base()],
        dict(DEFAULT_TRADING_CONTROLS),
        PaperExecutionControls.model_validate(DEFAULT_PAPER_EXECUTION_CONTROLS),
        get_settings(),
    )
    assert result.trades


def test_one_shared_pass_gives_the_same_answer_as_one_pass_each(replay_inputs) -> None:
    """The whole claim. A score grid that discriminates: 50 signals early and
    96 never does, so the pending-strategy bookkeeping is exercised in both
    directions within one pass."""
    scores = [50, 96]
    shared = _sweep(replay_inputs, {"minimum_score": scores})
    separate = _sweep(replay_inputs, {"minimum_score": scores, "risk_per_trade_percent": [1.0]})

    assert len(shared.combinations) == len(separate.combinations) == 2
    for mine, theirs in zip(shared.combinations, separate.combinations, strict=True):
        assert mine.parameters["minimum_score"] == theirs.parameters["minimum_score"]
        assert mine.in_sample == theirs.in_sample
        assert mine.validation == theirs.validation
        assert mine.proven == theirs.proven


def test_the_variants_do_not_leak_into_each_other(replay_inputs) -> None:
    """A lower threshold must take at least as many trades as a higher one.
    If state or trades crossed between variants in the shared pass, this is
    where it would show."""
    result = _sweep(replay_inputs, {"minimum_score": [50, 72, 96]})
    counts = [item.in_sample["trades"] + item.validation["trades"] for item in result.combinations]
    assert counts == sorted(counts, reverse=True)
    assert counts[0] > 0
    assert counts[-1] == 0


def test_a_grid_touching_the_controls_still_runs_one_at_a_time(replay_inputs) -> None:
    """Controls are taken once for a whole replay, so those combinations
    cannot share one. The result must still be complete."""
    result = _sweep(replay_inputs, {"min_stop_distance_percent": [0.35, 0.8]})
    assert len(result.combinations) == 2
    assert {item.parameters["min_stop_distance_percent"] for item in result.combinations} == {0.35, 0.8}


def test_a_single_combination_is_not_routed_through_the_shared_pass(replay_inputs) -> None:
    """Nothing to share, and the one-pass path would rename the strategy for
    no reason."""
    result = _sweep(replay_inputs, {"minimum_score": [50]})
    assert len(result.combinations) == 1
    assert result.combinations[0].parameters == {"minimum_score": 50}
