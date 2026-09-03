from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services.extra_strategies import (
    AWAITING,
    LONG_PULLBACK,
    SIGNALLED,
    evaluate_ema_momentum,
    evaluate_vwap_pullback,
)
from app.services.market_calculations import CompletedCandle

CONTROLS = {
    "account_capital": 100_000.0,
    "risk_per_trade_percent": 0.5,
    "minimum_score": 60,
    "minimum_rr": 1.5,
    "volume_multiplier": 1.3,
    "retest_tolerance_percent": 0.15,
    "trade_start_time": "09:24",
    "trade_cutoff_time": "14:45",
}
NIFTY = {"nifty_regime": {"regime": "BULLISH"}}


def _candle(open_p: str, high: str, low: str, close: str) -> CompletedCandle:
    opened_at = datetime(2026, 8, 31, 4, 2, tzinfo=UTC)  # 09:32 Asia/Kolkata
    return CompletedCandle(
        instrument_token="NSE:2885",
        timeframe_seconds=60,
        opened_at=opened_at,
        closed_at=opened_at + timedelta(minutes=1),
        open=Decimal(open_p),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=500,
        tick_count=10,
    )


VWAP_INDICATORS = {
    "vwap": 100.0,
    "ema_fast": 102.0,
    "ema_slow": 99.0,
    "atr": 2.0,
    "volume": {"relative_volume": 2.0},
    "relative_strength": {"relative_strength_percent": 0.5},
}


def test_vwap_pullback_needs_a_pullback_then_a_reclaim() -> None:
    pull = evaluate_vwap_pullback(_candle("100.5", "101", "99.8", "101"), VWAP_INDICATORS, NIFTY, CONTROLS, AWAITING)
    assert pull.next_state == LONG_PULLBACK and pull.side is None

    signal = evaluate_vwap_pullback(
        _candle("100.2", "101.6", "99.9", "100.8"), VWAP_INDICATORS, NIFTY, CONTROLS, LONG_PULLBACK
    )
    assert signal.next_state == SIGNALLED
    assert signal.side == "LONG"
    assert signal.quantity > 0
    assert signal.entry_price is not None and signal.target_price is not None
    assert signal.target_price > signal.entry_price
    assert signal.stop_price < signal.entry_price


def test_vwap_pullback_resets_when_the_trend_is_lost() -> None:
    flat = {**VWAP_INDICATORS, "ema_fast": 99.0, "ema_slow": 99.5}  # fast below slow
    decision = evaluate_vwap_pullback(_candle("100.2", "101.6", "99.9", "100.8"), flat, NIFTY, CONTROLS, LONG_PULLBACK)
    assert decision.next_state == AWAITING
    assert decision.reason == "Trend alignment was lost"


def test_vwap_pullback_is_not_ready_without_indicators() -> None:
    decision = evaluate_vwap_pullback(_candle("100", "101", "99", "100"), {"vwap": 100.0}, NIFTY, CONTROLS, AWAITING)
    assert decision.next_state == AWAITING
    assert decision.reason == "Trend and VWAP indicators are not ready"


EMA_INDICATORS = {
    "vwap": 99.0,
    "ema_fast": 99.5,
    "ema_slow": 99.0,
    "atr": 1.5,
    "extension_atr": 1.0,
    "opening_range": {"high": 100.0, "low": 98.0, "complete": True},
    "volume": {"relative_volume": 2.0},
    "relative_strength": {"relative_strength_percent": 0.5},
}


def test_ema_momentum_signals_on_a_fresh_push_through_the_opening_range() -> None:
    decision = evaluate_ema_momentum(_candle("99.8", "101", "99.6", "100.5"), EMA_INDICATORS, NIFTY, CONTROLS, AWAITING)
    assert decision.next_state == SIGNALLED
    assert decision.side == "LONG"
    assert decision.quantity > 0
    assert decision.score >= CONTROLS["minimum_score"]
    assert decision.stop_price < decision.entry_price < decision.target_price


def test_ema_momentum_stands_aside_when_price_is_already_extended() -> None:
    extended = {**EMA_INDICATORS, "extension_atr": 3.0}
    decision = evaluate_ema_momentum(_candle("99.8", "101", "99.6", "100.5"), extended, NIFTY, CONTROLS, AWAITING)
    assert decision.next_state == AWAITING
    assert decision.reason == "Price is already extended from VWAP"


def test_ema_momentum_is_idempotent_once_signalled() -> None:
    decision = evaluate_ema_momentum(
        _candle("99.8", "101", "99.6", "100.5"), EMA_INDICATORS, NIFTY, CONTROLS, SIGNALLED
    )
    assert decision.next_state == SIGNALLED
    assert decision.side is None


def test_registry_exposes_all_three_strategies_and_routes_by_type() -> None:
    from app.services.strategy_registry import StrategyConfiguration, StrategyRegistry

    identifiers = {item.identifier for item in StrategyRegistry.metadata()}
    assert identifiers == {"orb-retest-v1", "vwap-pullback-v1", "ema-momentum-v1"}

    strategy = StrategyConfiguration(name="VWAP Pullback A", strategy_type="vwap-pullback-v1")
    replayed = StrategyRegistry.evaluate(
        strategy, _candle("100.5", "101", "99.8", "101"), VWAP_INDICATORS, NIFTY, CONTROLS, AWAITING
    )
    assert replayed.next_state == LONG_PULLBACK
    direct = evaluate_vwap_pullback(_candle("100.5", "101", "99.8", "101"), VWAP_INDICATORS, NIFTY, CONTROLS, AWAITING)
    assert replayed == direct
