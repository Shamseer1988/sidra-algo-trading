"""Deterministic, paper-only opening-range breakout/retest strategy.

This module deliberately returns decisions rather than submitting orders.  Given the
same completed candle, indicators, controls, and prior state, it always returns the
same result and never accesses wall-clock time or broker services.
"""

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Literal

from app.services.market_calculations import MARKET_TIMEZONE, CompletedCandle

STRATEGY_VERSION = "orb-retest-v1"
AWAITING = "AWAITING_BREAKOUT"
LONG_BREAKOUT = "LONG_BREAKOUT"
SHORT_BREAKOUT = "SHORT_BREAKOUT"
SIGNALLED = "SIGNALLED"
Side = Literal["LONG", "SHORT"]


@dataclass(frozen=True)
class StrategyDecision:
    next_state: str
    side: Side | None = None
    entry_price: Decimal | None = None
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    quantity: int = 0
    risk_amount: Decimal | None = None
    score: int = 0
    score_breakdown: dict[str, int] | None = None
    reason: str | None = None


def _number(values: dict, key: str) -> Decimal | None:
    value = values.get(key)
    try:
        return Decimal(str(value)) if value is not None else None
    except (ArithmeticError, TypeError, ValueError):
        return None


def _inside_trade_window(candle: CompletedCandle, controls: dict) -> bool:
    opened = candle.opened_at.astimezone(MARKET_TIMEZONE).strftime("%H:%M")
    closed = candle.closed_at.astimezone(MARKET_TIMEZONE).strftime("%H:%M")
    return str(controls["trade_start_time"]) <= opened and closed <= str(controls["trade_cutoff_time"])


def _is_choppy(candle: CompletedCandle, indicators: dict, controls: dict) -> bool:
    fast = _number(indicators, "ema_fast")
    slow = _number(indicators, "ema_slow")
    if fast is None or slow is None or candle.close <= 0:
        return True
    spread_percent = abs(fast - slow) * Decimal("100") / candle.close
    return spread_percent < Decimal(str(controls.get("minimum_ema_spread_percent", 0.05)))


def _score(side: Side, candle: CompletedCandle, indicators: dict, nifty: dict, controls: dict) -> dict[str, int]:
    is_long = side == "LONG"
    vwap = _number(indicators, "vwap")
    fast = _number(indicators, "ema_fast")
    slow = _number(indicators, "ema_slow")
    volume = indicators.get("volume") if isinstance(indicators.get("volume"), dict) else {}
    relative = indicators.get("relative_strength") if isinstance(indicators.get("relative_strength"), dict) else {}
    relative_value = _number(relative, "relative_strength_percent")
    regime = nifty.get("nifty_regime") if isinstance(nifty.get("nifty_regime"), dict) else {}
    regime_name = regime.get("regime")
    directional_vwap = bool(vwap is not None and (candle.close > vwap if is_long else candle.close < vwap))
    directional_ema = bool(fast is not None and slow is not None and (fast > slow if is_long else fast < slow))
    relative_ok = bool(relative_value is not None and (relative_value > 0 if is_long else relative_value < 0))
    regime_ok = regime_name == ("BULLISH" if is_long else "BEARISH")
    relative_volume = _number(volume, "relative_volume")
    volume_ok = bool(relative_volume is not None and relative_volume >= Decimal(str(controls["volume_multiplier"])))
    return {
        "breakout_retest": 20,
        "vwap_alignment": 20 if directional_vwap else 0,
        "ema_alignment": 20 if directional_ema else 0,
        "volume_confirmation": 20 if volume_ok else 0,
        "market_confirmation": 20 if relative_ok and regime_ok else 0,
    }


def _risk_plan(
    side: Side,
    candle: CompletedCandle,
    breakout_level: Decimal,
    controls: dict,
) -> tuple[Decimal, Decimal, int, Decimal] | None:
    tolerance = Decimal(str(controls["retest_tolerance_percent"])) / Decimal("100")
    if side == "LONG":
        entry = candle.close
        stop = min(candle.low, breakout_level * (Decimal("1") - tolerance))
        risk_per_unit = entry - stop
        target = entry + (risk_per_unit * Decimal(str(controls["minimum_rr"])))
    else:
        entry = candle.close
        stop = max(candle.high, breakout_level * (Decimal("1") + tolerance))
        risk_per_unit = stop - entry
        target = entry - (risk_per_unit * Decimal(str(controls["minimum_rr"])))
    if risk_per_unit <= 0:
        return None
    risk_amount = (
        Decimal(str(controls["account_capital"])) * Decimal(str(controls["risk_per_trade_percent"])) / Decimal("100")
    )
    quantity = int((risk_amount / risk_per_unit).to_integral_value(rounding=ROUND_DOWN))
    if quantity < 1:
        return None
    return stop, target, quantity, risk_amount


def evaluate_orb_retest(
    candle: CompletedCandle,
    indicators: dict,
    nifty_indicators: dict,
    controls: dict,
    prior_state: str = AWAITING,
) -> StrategyDecision:
    """Advance a long/short state machine using exactly one completed candle."""
    opening = indicators.get("opening_range") if isinstance(indicators.get("opening_range"), dict) else {}
    high = _number(opening, "high")
    low = _number(opening, "low")
    if not _inside_trade_window(candle, controls):
        return StrategyDecision(next_state=AWAITING, reason="Outside configured trade window")
    if not opening.get("complete") or high is None or low is None or high <= low:
        return StrategyDecision(next_state=AWAITING, reason="Opening range is incomplete")
    if prior_state == SIGNALLED:
        return StrategyDecision(next_state=SIGNALLED, reason="Instrument already signalled this session")
    if prior_state not in {AWAITING, LONG_BREAKOUT, SHORT_BREAKOUT}:
        return StrategyDecision(next_state=AWAITING, reason="Unknown strategy state was reset")
    tolerance = Decimal(str(controls["retest_tolerance_percent"])) / Decimal("100")
    if prior_state == AWAITING:
        if candle.close > high:
            return StrategyDecision(
                next_state=LONG_BREAKOUT,
                reason="Long breakout observed; awaiting retest",
            )
        if candle.close < low:
            return StrategyDecision(next_state=SHORT_BREAKOUT, reason="Short breakout observed; awaiting retest")
        return StrategyDecision(next_state=AWAITING, reason="Awaiting opening-range breakout")
    side: Side = "LONG" if prior_state == LONG_BREAKOUT else "SHORT"
    level = high if side == "LONG" else low
    if side == "LONG":
        retested = candle.low <= level * (Decimal("1") + tolerance) and candle.close > level
        invalidated = candle.close < low
    else:
        retested = candle.high >= level * (Decimal("1") - tolerance) and candle.close < level
        invalidated = candle.close > high
    if invalidated:
        return StrategyDecision(next_state=AWAITING, reason="Breakout was invalidated")
    if not retested:
        return StrategyDecision(next_state=prior_state, reason="Awaiting breakout retest")
    if _is_choppy(candle, indicators, controls):
        return StrategyDecision(next_state=AWAITING, reason="EMA spread indicates choppy market")
    breakdown = _score(side, candle, indicators, nifty_indicators, controls)
    score = sum(breakdown.values())
    if score < int(controls["minimum_score"]):
        return StrategyDecision(
            next_state=AWAITING,
            score=score,
            score_breakdown=breakdown,
            reason="Retest failed score threshold",
        )
    plan = _risk_plan(side, candle, level, controls)
    if plan is None:
        return StrategyDecision(
            next_state=AWAITING,
            score=score,
            score_breakdown=breakdown,
            reason="Risk plan is invalid",
        )
    stop, target, quantity, risk_amount = plan
    return StrategyDecision(
        next_state=SIGNALLED,
        side=side,
        entry_price=candle.close,
        stop_price=stop,
        target_price=target,
        quantity=quantity,
        risk_amount=risk_amount,
        score=score,
        score_breakdown=breakdown,
        reason="Paper signal confirmed",
    )
