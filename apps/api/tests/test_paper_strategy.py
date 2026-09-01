from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services.market_calculations import CompletedCandle
from app.services.paper_strategy import AWAITING, LONG_BREAKOUT, SIGNALLED, evaluate_orb_retest
from app.services.strategy_registry import StrategyConfiguration

CONTROLS = {
    "account_capital": 100_000.0,
    "risk_per_trade_percent": 0.5,
    "maximum_daily_risk_percent": 1.0,
    "maximum_signals": 2,
    "minimum_score": 90,
    "minimum_rr": 1.5,
    "volume_multiplier": 1.3,
    "retest_tolerance_percent": 0.15,
    "trade_start_time": "09:24",
    "trade_cutoff_time": "14:45",
}

INDICATORS = {
    "opening_range": {"high": 110.0, "low": 100.0, "complete": True},
    "vwap": 105.0,
    "ema_fast": 111.0,
    "ema_slow": 106.0,
    "volume": {"relative_volume": 1.5},
    "relative_strength": {"relative_strength_percent": 0.4},
}
NIFTY = {"nifty_regime": {"regime": "BULLISH"}}


def candle(*, close: str, low: str, high: str) -> CompletedCandle:
    opened_at = datetime(2026, 8, 31, 4, 2, tzinfo=UTC)  # 09:32 Asia/Kolkata
    return CompletedCandle(
        instrument_token="NSE:2885",
        timeframe_seconds=60,
        opened_at=opened_at,
        closed_at=opened_at + timedelta(minutes=1),
        open=Decimal("110.5"),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=500,
        tick_count=10,
    )


def test_long_strategy_requires_breakout_then_retest_before_paper_signal() -> None:
    breakout = evaluate_orb_retest(candle(close="111", low="110.8", high="112"), INDICATORS, NIFTY, CONTROLS, AWAITING)
    assert breakout.next_state == LONG_BREAKOUT
    assert breakout.side is None

    confirmed = evaluate_orb_retest(
        candle(close="112", low="110.1", high="112.5"), INDICATORS, NIFTY, CONTROLS, LONG_BREAKOUT
    )
    assert confirmed.next_state == SIGNALLED
    assert confirmed.side == "LONG"
    assert confirmed.score == 100
    assert confirmed.quantity > 0
    assert confirmed.target_price is not None and confirmed.entry_price is not None
    assert confirmed.target_price > confirmed.entry_price


def test_strategy_rejects_retest_when_market_confirmation_fails() -> None:
    bearish_nifty = {"nifty_regime": {"regime": "BEARISH"}}
    decision = evaluate_orb_retest(
        candle(close="112", low="110.1", high="112.5"), INDICATORS, bearish_nifty, CONTROLS, LONG_BREAKOUT
    )
    assert decision.next_state == AWAITING
    assert decision.score == 80
    assert decision.side is None


def test_strategy_rejects_choppy_ema_retest_and_candles_past_cutoff() -> None:
    choppy = {**INDICATORS, "ema_fast": 110.01, "ema_slow": 110.0}
    decision = evaluate_orb_retest(
        candle(close="112", low="110.1", high="112.5"), choppy, NIFTY, CONTROLS, LONG_BREAKOUT
    )
    assert decision.next_state == AWAITING
    assert decision.reason == "EMA spread indicates choppy market"

    late = candle(close="112", low="110.1", high="112.5")
    late = CompletedCandle(
        **{
            **late.__dict__,
            "opened_at": datetime(2026, 8, 31, 9, 16, tzinfo=UTC),
            "closed_at": datetime(2026, 8, 31, 9, 17, tzinfo=UTC),
        }
    )
    assert (
        evaluate_orb_retest(late, INDICATORS, NIFTY, CONTROLS, LONG_BREAKOUT).reason
        == "Outside configured trade window"
    )


def test_strategy_resets_unknown_state_and_handles_invalid_risk_plan() -> None:
    reset = evaluate_orb_retest(candle(close="111", low="110.8", high="112"), INDICATORS, NIFTY, CONTROLS, "CORRUPTED")
    assert reset.next_state == AWAITING
    assert reset.reason == "Unknown strategy state was reset"

    no_risk = evaluate_orb_retest(
        candle(close="112", low="110.1", high="112.5"),
        INDICATORS,
        NIFTY,
        {**CONTROLS, "account_capital": 1},
        LONG_BREAKOUT,
    )
    assert no_risk.next_state == AWAITING


def test_registry_configuration_overrides_global_strategy_parameters() -> None:
    strategy = StrategyConfiguration(
        name="Conservative ORB",
        minimum_score=100,
        minimum_rr=2.25,
        volume_multiplier=1.8,
    )
    effective = strategy.effective_controls(CONTROLS)

    assert effective["minimum_score"] == 100
    assert effective["minimum_rr"] == 2.25
    assert effective["volume_multiplier"] == 1.8
    assert CONTROLS["minimum_score"] == 90
