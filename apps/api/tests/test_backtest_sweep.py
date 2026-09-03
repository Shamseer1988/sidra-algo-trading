from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.services.backtest_sweep import build_combinations, run_parameter_sweep
from app.services.market_calculations import CompletedCandle
from app.services.paper_execution import PaperExecutionControls
from app.services.strategy_registry import StrategyConfiguration

IST = ZoneInfo("Asia/Kolkata")


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        opening_range_minutes=15,
        ema_fast_period=9,
        ema_slow_period=21,
        volume_lookback_candles=20,
        atr_period=14,
        daily_history_sessions=40,
    )


def _controls() -> dict:
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
    }


def _strategy() -> StrategyConfiguration:
    return StrategyConfiguration(
        id="sweep-base",
        name="Sweep Base",
        universe=["NSE:TEST"],
        minimum_score=0,
        minimum_rr=1,
        volume_multiplier=0.5,
        minimum_ema_spread_percent=0,
    )


def _c(instrument: str, opened_at: datetime, o: str, h: str, low: str, c: str) -> CompletedCandle:
    return CompletedCandle(
        instrument_token=instrument,
        timeframe_seconds=60,
        opened_at=opened_at,
        closed_at=opened_at + timedelta(minutes=1),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=1_000,
        tick_count=10,
    )


def _day(day: date, instrument: str) -> list[CompletedCandle]:
    start = datetime.combine(day, time(9, 15), tzinfo=IST).astimezone(UTC)
    rows = [_c(instrument, start + timedelta(minutes=i), "100", "101", "99", "100") for i in range(21)]
    rows.append(_c(instrument, start + timedelta(minutes=21), "100", "102.5", "101.5", "102"))
    rows.append(_c(instrument, start + timedelta(minutes=22), "102", "102.5", "101.1", "102"))
    rows.append(_c(instrument, start + timedelta(minutes=23), "102", "102.5", "101.5", "102.3"))
    rows.append(_c(instrument, start + timedelta(minutes=24), "102.3", "104", "101.5", "103.5"))
    return rows


DAYS = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]
_EXECUTION = PaperExecutionControls(
    slippage_bps=0,
    brokerage_percent=0,
    stt_sell_percent=0,
    exchange_transaction_percent=0,
    gst_percent=0,
    sebi_percent=0,
    stamp_duty_buy_percent=0,
)


def _history() -> tuple[dict[str, list[CompletedCandle]], list[CompletedCandle]]:
    stock = [candle for day in DAYS for candle in _day(day, "NSE:TEST")]
    benchmark = [candle for day in DAYS for candle in _day(day, "NSE:26000")]
    return {"NSE:TEST": stock}, benchmark


def test_build_combinations_rejects_unknown_params_and_oversized_grids() -> None:
    with pytest.raises(ValueError, match="Unsupported sweep parameter"):
        build_combinations({"not_a_param": [1]}, 40)
    with pytest.raises(ValueError, match="expands to 12 combinations"):
        build_combinations({"minimum_rr": [1, 2, 3], "volume_multiplier": [1, 2, 3, 4]}, 6)
    assert len(build_combinations({"minimum_rr": [1.0, 2.0], "atr_period": [10, 14]}, 40)) == 4


def test_run_parameter_sweep_is_deterministic_and_splits_in_sample_from_validation() -> None:
    by_instrument, benchmark = _history()
    grid = {"minimum_rr": [1.0, 2.0], "volume_multiplier": [0.5, 1.0]}

    first = run_parameter_sweep(
        _strategy(), _controls(), _EXECUTION, _settings(), by_instrument, benchmark, grid, validation_fraction=0.5
    )
    second = run_parameter_sweep(
        _strategy(), _controls(), _EXECUTION, _settings(), by_instrument, benchmark, grid, validation_fraction=0.5
    )
    assert [c.as_dict() for c in first.combinations] == [c.as_dict() for c in second.combinations]

    assert len(first.combinations) == 4
    assert first.best_index in range(4)
    for combo in first.combinations:
        assert set(combo.parameters) == {"minimum_rr", "volume_multiplier"}
        assert combo.in_sample["trades"] >= 0 and combo.validation["trades"] >= 0
    # The four trading days are split, so both blocks see trades.
    assert any(c.in_sample["trades"] > 0 for c in first.combinations)
    assert any(c.validation["trades"] > 0 for c in first.combinations)


def test_run_parameter_sweep_prefers_a_validated_combination() -> None:
    by_instrument, benchmark = _history()
    grid = {"minimum_rr": [1.0, 5.0]}
    result = run_parameter_sweep(
        _strategy(), _controls(), _EXECUTION, _settings(), by_instrument, benchmark, grid, validation_fraction=0.5
    )
    best = next(c for c in result.combinations if c.index == result.best_index)
    proven = [c for c in result.combinations if c.proven]
    if proven:
        assert best.proven
