from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services.market_calculations import CompletedCandle
from app.services.paper_strategy import AWAITING, LONG_BREAKOUT, SIGNALLED, evaluate_orb_retest
from app.services.strategy_registry import StrategyConfiguration, StrategyRegistry

CONTROLS = {
    "account_capital": 100_000.0,
    "risk_per_trade_percent": 0.5,
    "maximum_daily_risk_percent": 1.0,
    "maximum_signals": 2,
    "minimum_score": 80,
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
    "volume": {"relative_volume": 3.0},
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


def test_strategy_rejects_retest_when_market_conditions_fail() -> None:
    hostile_regime = {"nifty_regime": {"regime": "BEARISH"}}
    weak_stock = {**INDICATORS, "relative_strength": {"relative_strength_percent": -0.6}}
    controls = {**CONTROLS, "minimum_score": 90}
    decision = evaluate_orb_retest(
        candle(close="112", low="110.1", high="112.5"), weak_stock, hostile_regime, controls, LONG_BREAKOUT
    )
    assert decision.next_state == AWAITING
    assert decision.reason == "Retest failed score threshold"
    assert decision.side is None
    assert decision.score == 80
    assert decision.score_breakdown["market_confirmation"] == 0


def test_score_awards_partial_credit_for_marginal_confirmation() -> None:
    controls = {**CONTROLS, "minimum_score": 0}
    marginal = {
        **INDICATORS,
        "atr": 4.0,
        "opening_range_atr": 1.0,
        "volume": {"relative_volume": 1.35},  # barely over the 1.3 multiple
        "relative_strength": {"relative_strength_percent": 0.1},  # weakly positive
    }
    decision = evaluate_orb_retest(
        candle(close="112", low="110.1", high="112.5"), marginal, NIFTY, controls, LONG_BREAKOUT
    )
    breakdown = decision.score_breakdown
    assert 0 < breakdown["volume_confirmation"] < 20  # partial, not all-or-nothing
    assert 10 <= breakdown["market_confirmation"] < 20  # regime aligned, relative strength weak
    assert breakdown["breakout_retest"] <= 20


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


def test_risk_plan_widens_a_tight_stop_to_the_atr_floor() -> None:
    controls = {
        **CONTROLS,
        "account_capital": 20_000.0,
        "risk_per_trade_percent": 1.0,
        "minimum_rr": 2.0,
        "stop_atr_multiple": 1.5,
        "min_stop_distance_percent": 0.5,
        "maximum_open_exposure_percent": 100.0,
        "intraday_leverage_enabled": True,
        "intraday_leverage_multiplier": 5.0,
    }
    indicators = {**INDICATORS, "atr": 3.0}
    decision = evaluate_orb_retest(
        candle(close="112", low="110.1", high="112.5"), indicators, NIFTY, controls, LONG_BREAKOUT
    )
    assert decision.next_state == SIGNALLED
    # Structural distance is ~2.165; the 1.5 x ATR (4.5) floor dominates.
    assert decision.entry_price is not None and decision.stop_price is not None
    assert decision.entry_price - decision.stop_price == Decimal("4.5")
    assert decision.target_price == decision.entry_price + Decimal("9.0")  # RR 2.0
    # Risk-budget quantity: floor((20000 * 1%) / 4.5) = 44; exposure cap is far higher.
    assert decision.quantity == 44


def test_risk_plan_exposure_cap_binds_before_the_risk_budget() -> None:
    controls = {
        **CONTROLS,
        "account_capital": 5_000.0,
        "risk_per_trade_percent": 5.0,
        "maximum_open_exposure_percent": 100.0,
        "intraday_leverage_enabled": False,
        "stop_atr_multiple": 0.0,
        "min_stop_distance_percent": 0.0,
    }
    decision = evaluate_orb_retest(
        candle(close="112", low="110.1", high="112.5"), INDICATORS, NIFTY, controls, LONG_BREAKOUT
    )
    assert decision.next_state == SIGNALLED
    # Risk budget allows ~115 shares, but 1x cash exposure of 5000 / 112 caps it at 44.
    assert decision.quantity == 44


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


def test_strategy_configuration_defaults_align_with_trading_controls() -> None:
    from app.api.routes.settings import DEFAULT_TRADING_CONTROLS

    default = StrategyConfiguration(name="Default ORB")
    assert default.minimum_score == DEFAULT_TRADING_CONTROLS["minimum_score"]
    # Per-side cap is opt-in; when unset the per-day cap is the only trade limit.
    assert default.max_trades_per_side is None

    capped = StrategyConfiguration(name="Long-limited ORB", max_trades_per_side=1)
    assert capped.max_trades_per_side == 1


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
    assert CONTROLS["minimum_score"] == 80


def test_versioned_registry_replays_orb_deterministically_with_an_immutable_snapshot() -> None:
    strategy = StrategyConfiguration(name="ORB Replay", universe=["NSE:2885"])
    effective = strategy.effective_controls(CONTROLS)
    breakout = StrategyRegistry.evaluate(
        strategy, candle(close="111", low="110.8", high="112"), INDICATORS, NIFTY, effective, AWAITING
    )
    replayed = StrategyRegistry.evaluate(
        strategy,
        candle(close="112", low="110.1", high="112.5"),
        INDICATORS,
        NIFTY,
        effective,
        breakout.next_state,
    )
    direct = evaluate_orb_retest(
        candle(close="112", low="110.1", high="112.5"), INDICATORS, NIFTY, effective, breakout.next_state
    )

    assert replayed == direct
    assert replayed.next_state == SIGNALLED
    assert strategy.snapshot(CONTROLS) == {
        "configuration": strategy.model_dump(),
        "effective_controls": effective,
    }


def test_registry_enforces_scope_session_and_directional_configuration() -> None:
    out_of_scope = StrategyConfiguration(name="Scoped ORB", universe=["NSE:OTHER"])
    decision = StrategyRegistry.evaluate(
        out_of_scope, candle(close="111", low="110.8", high="112"), INDICATORS, NIFTY, CONTROLS, AWAITING
    )
    assert decision.reason == "Instrument is outside this strategy universe"

    session_disabled = StrategyConfiguration(name="Paused Session", allowed_sessions=[])
    decision = StrategyRegistry.evaluate(
        session_disabled, candle(close="111", low="110.8", high="112"), INDICATORS, NIFTY, CONTROLS, AWAITING
    )
    assert decision.reason == "Regular-session signals are disabled"

    short_only = StrategyConfiguration(name="Short Only", allowed_sides=["SHORT"])
    decision = StrategyRegistry.evaluate(
        short_only,
        candle(close="112", low="110.1", high="112.5"),
        INDICATORS,
        NIFTY,
        CONTROLS,
        LONG_BREAKOUT,
    )
    assert decision.reason == "Long signals are disabled"
