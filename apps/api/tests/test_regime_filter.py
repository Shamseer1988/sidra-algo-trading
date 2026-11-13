"""The NIFTY regime without volume, and the filter that trades only with it."""

from dataclasses import replace
from decimal import Decimal

from app.services.market_calculations import nifty_regime
from app.services.paper_strategy import AWAITING, SIGNALLED, StrategyDecision
from app.services.strategy_registry import regime_filtered
from tests.test_market_calculations import make_candle


def index_day(step: int) -> list:
    """An index day: 21 one-minute candles, volume 0 as the feed delivers an index."""
    return [make_candle(index, str(25000 + index * step), volume=0) for index in range(21)]


def test_an_index_with_no_volume_still_has_a_rising_day() -> None:
    regime = nifty_regime(index_day(+5), 9, 21)
    assert regime["regime"] == "BULLISH"
    assert regime["vwap"] is None
    assert regime["anchor"] == "SESSION_OPEN"


def test_an_index_with_no_volume_still_has_a_falling_day() -> None:
    assert nifty_regime(index_day(-5), 9, 21)["regime"] == "BEARISH"


def test_a_rising_ema_below_the_open_is_not_a_rising_day() -> None:
    candles = index_day(+5)
    # The day opened far above where it now trades: EMAs still rising, price under the open.
    candles[0] = replace(candles[0], open=Decimal("26000"))
    assert nifty_regime(candles, 9, 21)["regime"] == "NEUTRAL"


def test_vwap_is_still_the_anchor_when_there_is_volume() -> None:
    candles = [make_candle(index, str(100 + index * 2)) for index in range(21)]
    regime = nifty_regime(candles, 9, 21)
    assert regime["regime"] == "BULLISH"
    assert regime["anchor"] == "VWAP"


def test_too_few_candles_is_still_insufficient() -> None:
    assert nifty_regime(index_day(+5)[:20], 9, 21)["regime"] == "INSUFFICIENT_DATA"


def signal(side: str) -> StrategyDecision:
    return StrategyDecision(
        next_state=SIGNALLED,
        side=side,
        entry_price=Decimal("100"),
        stop_price=Decimal("99"),
        target_price=Decimal("102"),
        quantity=10,
        risk_amount=Decimal("10"),
        score=76,
        score_breakdown={"market_confirmation": 0},
        reason="Paper signal confirmed",
    )


def bench(regime: str | None) -> dict:
    return {"nifty_regime": {"regime": regime}} if regime else {}


ON = {"regime_filter_enabled": True}


def test_filter_off_changes_nothing() -> None:
    decision = signal("LONG")
    assert regime_filtered(decision, bench("BEARISH"), {"regime_filter_enabled": False}) is decision
    assert regime_filtered(decision, bench("BEARISH"), {}) is decision


def test_a_long_on_a_falling_day_is_refused() -> None:
    refused = regime_filtered(signal("LONG"), bench("BEARISH"), ON)
    assert refused.next_state == AWAITING
    assert refused.side is None
    assert refused.score == 76
    assert "BEARISH" in refused.reason and "BULLISH" in refused.reason


def test_a_short_on_a_falling_day_is_taken() -> None:
    decision = signal("SHORT")
    assert regime_filtered(decision, bench("BEARISH"), ON) is decision


def test_a_long_on_a_rising_day_is_taken() -> None:
    decision = signal("LONG")
    assert regime_filtered(decision, bench("BULLISH"), ON) is decision


def test_a_neutral_or_unknown_index_takes_nothing() -> None:
    for regime in ("NEUTRAL", "INSUFFICIENT_DATA", None):
        assert regime_filtered(signal("LONG"), bench(regime), ON).next_state == AWAITING
        assert regime_filtered(signal("SHORT"), bench(regime), ON).next_state == AWAITING


def test_a_decision_that_is_not_a_signal_passes_untouched() -> None:
    waiting = StrategyDecision(next_state=AWAITING, reason="Awaiting a relative-strength pullback")
    assert regime_filtered(waiting, bench("BEARISH"), ON) is waiting
