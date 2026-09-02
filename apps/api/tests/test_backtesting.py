from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from app.services.backtesting import run_completed_candle_backtest
from app.services.market_calculations import CompletedCandle
from app.services.paper_execution import PaperExecutionControls
from app.services.strategy_registry import StrategyConfiguration


def _candle(index: int, *, instrument: str, open_price: str, high: str, low: str, close: str) -> CompletedCandle:
    opened_at = datetime(2026, 1, 5, 3, 45, tzinfo=UTC) + timedelta(minutes=index)
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


def _series(instrument: str) -> list[CompletedCandle]:
    rows = [
        _candle(index, instrument=instrument, open_price="100", high="101", low="99", close="100")
        for index in range(21)
    ]
    rows.extend(
        [
            _candle(21, instrument=instrument, open_price="100", high="102.5", low="101.5", close="102"),
            _candle(22, instrument=instrument, open_price="102", high="102.5", low="101.1", close="102"),
            _candle(23, instrument=instrument, open_price="102", high="102.5", low="101.5", close="102.3"),
            _candle(24, instrument=instrument, open_price="102.3", high="104", low="101.5", close="103.5"),
        ]
    )
    return rows


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        opening_range_minutes=15,
        ema_fast_period=9,
        ema_slow_period=21,
        volume_lookback_candles=20,
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
        id="orb-historical-test",
        name="ORB Historical Test",
        universe=["NSE:TEST"],
        minimum_score=0,
        minimum_rr=1,
        volume_multiplier=0.5,
        minimum_ema_spread_percent=0,
    )


def test_backtest_is_deterministic_and_fills_only_on_the_candle_after_the_signal() -> None:
    stock = _series("NSE:TEST")
    benchmark = _series("NSE:26000")
    execution = PaperExecutionControls(
        slippage_bps=0,
        brokerage_percent=0,
        stt_sell_percent=0,
        exchange_transaction_percent=0,
        gst_percent=0,
        sebi_percent=0,
        stamp_duty_buy_percent=0,
    )

    first = run_completed_candle_backtest(
        {"NSE:TEST": stock}, benchmark, [_strategy()], _controls(), execution, _settings()
    )
    second = run_completed_candle_backtest(
        {"NSE:TEST": stock}, benchmark, [_strategy()], _controls(), execution, _settings()
    )

    assert first == second
    assert len(first.trades) == 1
    trade = first.trades[0]
    assert trade.signal_at == stock[22].closed_at
    assert trade.entered_at == stock[23].opened_at
    assert trade.exited_at == stock[24].closed_at
    assert trade.exit_reason == "TARGET"
    assert first.summary["trades"] == 1


def test_future_candles_cannot_change_a_closed_historical_trade() -> None:
    stock = _series("NSE:TEST")
    benchmark = _series("NSE:26000")
    execution = PaperExecutionControls(
        slippage_bps=0,
        brokerage_percent=0,
        stt_sell_percent=0,
        exchange_transaction_percent=0,
        gst_percent=0,
        sebi_percent=0,
        stamp_duty_buy_percent=0,
    )
    original = run_completed_candle_backtest(
        {"NSE:TEST": stock}, benchmark, [_strategy()], _controls(), execution, _settings()
    )
    altered = run_completed_candle_backtest(
        {"NSE:TEST": [*stock, _candle(25, instrument="NSE:TEST", open_price="100", high="500", low="1", close="2")]},
        [*benchmark, _candle(25, instrument="NSE:26000", open_price="100", high="500", low="1", close="2")],
        [_strategy()],
        _controls(),
        execution,
        _settings(),
    )

    assert altered.trades == original.trades
    assert altered.data_fingerprint != original.data_fingerprint
