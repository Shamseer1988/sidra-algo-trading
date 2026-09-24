"""Deterministic, paper-only opening-range breakout/retest strategy.

This module deliberately returns decisions rather than submitting orders.  Given the
same completed candle, indicators, controls, and prior state, it always returns the
same result and never accesses wall-clock time or broker services.
"""

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Literal

from app.services.exit_rules import from_controls as exit_rules_from
from app.services.exit_rules import plan as exit_plan
from app.services.market_calculations import MARKET_TIMEZONE, CompletedCandle
from app.services.signal_inputs import missing_required

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


def _chop_refusal(candle: CompletedCandle, indicators: dict, controls: dict) -> str | None:
    """Why this candle is not tradeable on EMA separation, or None if it is.

    Returns the reason rather than a boolean because the two cases it refuses
    are different facts and used to be reported as the same one. "EMA spread
    indicates choppy market" told an operator the market was ranging when what
    had actually happened was that no EMA had been computed — the same species
    of dishonesty as scoring a missing input full marks, in the field they read
    to find out why nothing traded.
    """
    fast = _number(indicators, "ema_fast")
    slow = _number(indicators, "ema_slow")
    if fast is None or slow is None:
        return "EMA values are unavailable"
    if candle.close <= 0:
        return "Candle close is not a usable price"
    spread_percent = abs(fast - slow) * Decimal("100") / candle.close
    if spread_percent < Decimal(str(controls.get("minimum_ema_spread_percent", 0.05))):
        return "EMA spread indicates choppy market"
    return None


def _clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return max(low, min(high, value))


def _round_points(value: Decimal, cap: Decimal = Decimal("20")) -> int:
    bounded = _clamp(value, Decimal("0"), cap)
    return int(bounded.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _score(
    side: Side,
    candle: CompletedCandle,
    indicators: dict,
    nifty: dict,
    controls: dict,
    level: Decimal,
) -> dict[str, int]:
    """Continuous, deterministic partial-credit scoring; each component is 0-20.

    **A missing input scores zero.** It used to score full credit, on the stated
    grounds that "the data-quality gate already blocks bad data" — which is not
    what that gate does. It watches the feed (tick freshness, missing buckets,
    latency) and says nothing about whether ATR or a volume baseline has been
    computed, so a healthy feed on a newly tracked instrument passed it with
    none of these available and then scored 100/100.

    Absence must not outscore unfavourable evidence. A strategy that genuinely
    depends on an input declares it required, and ``evaluate_orb_retest``
    refuses before reaching this function; everything else is optional and
    simply earns nothing when it is not there.
    """
    is_long = side == "LONG"
    close = candle.close
    twenty = Decimal("20")

    atr_value = _number(indicators, "atr")
    vwap = _number(indicators, "vwap")
    fast = _number(indicators, "ema_fast")
    slow = _number(indicators, "ema_slow")
    bands = indicators.get("vwap_bands") if isinstance(indicators.get("vwap_bands"), dict) else {}
    volume = indicators.get("volume") if isinstance(indicators.get("volume"), dict) else {}
    relative = indicators.get("relative_strength") if isinstance(indicators.get("relative_strength"), dict) else {}
    regime = nifty.get("nifty_regime") if isinstance(nifty.get("nifty_regime"), dict) else {}
    regime_name = regime.get("regime")
    opening_range_atr = _number(indicators, "opening_range_atr")
    extension_atr = _number(indicators, "extension_atr")

    # 1) Breakout / retest quality: how decisively price reclaimed the level, scaled by
    #    whether the opening range is a real range (measured in ATR units).
    if atr_value is not None and atr_value > 0:
        reclaim = (close - level) / atr_value if is_long else (level - close) / atr_value
        reclaim_points = Decimal("8") * _clamp(reclaim / Decimal("0.35"), Decimal("0"), Decimal("1"))
        range_factor = (
            _clamp(opening_range_atr / Decimal("0.75"), Decimal("0.4"), Decimal("1"))
            if opening_range_atr is not None and opening_range_atr > 0
            else Decimal("1")
        )
        breakout_points = (Decimal("8") + reclaim_points) * range_factor
    else:
        # No ATR means the reclaim cannot be measured, not that it was decisive.
        breakout_points = Decimal("0")

    # 2) Trend: EMA direction is a hard gate; separation beyond the choppy threshold is a bonus.
    directional_ema = fast is not None and slow is not None and (fast > slow if is_long else fast < slow)
    # No EMAs and EMAs pointing the wrong way both earn nothing. They are
    # different facts with the same worth: neither is evidence of a trend.
    if fast is None or slow is None or not directional_ema:
        ema_points = Decimal("0")
    else:
        spread_percent = abs(fast - slow) * Decimal("100") / close if close > 0 else Decimal("0")
        target = max(Decimal(str(controls.get("minimum_ema_spread_percent", 0.05))), Decimal("0.02")) * Decimal("4")
        ema_points = Decimal("10") + Decimal("10") * _clamp(spread_percent / target, Decimal("0"), Decimal("1"))

    # 3) VWAP: wrong side is a hard zero; otherwise reward being above VWAP but not overextended.
    upper_1 = _number(bands, "upper_1")
    upper_2 = _number(bands, "upper_2")
    lower_1 = _number(bands, "lower_1")
    lower_2 = _number(bands, "lower_2")
    # No VWAP, or on the wrong side of it: nothing either way.
    if vwap is None or (is_long and close < vwap) or (not is_long and close > vwap):
        vwap_points = Decimal("0")
    elif (is_long and upper_1 is None) or (not is_long and lower_1 is None):
        # On the right side of VWAP, but with no bands to say how extended it
        # is. Half credit: the direction is confirmed and the stretch is not.
        vwap_points = Decimal("10")
    else:
        near = upper_1 if is_long else lower_1
        far = upper_2 if is_long else lower_2
        within_near = (close <= near) if is_long else (close >= near)
        within_far = far is not None and ((close <= far) if is_long else (close >= far))
        vwap_points = twenty if within_near else (Decimal("13") if within_far else Decimal("6"))
    if extension_atr is not None and extension_atr > Decimal("2.5"):
        vwap_points = min(vwap_points, Decimal("8"))

    # 4) Volume: below the configured RVOL multiple is a hard zero; scale up to 2x the multiple.
    relative_volume = _number(volume, "relative_volume")
    threshold = Decimal(str(controls["volume_multiplier"]))
    # No baseline yet, or volume below the multiple: no confirmation either way.
    if relative_volume is None or relative_volume < threshold:
        volume_points = Decimal("0")
    else:
        excess = (relative_volume - threshold) / threshold if threshold > 0 else Decimal("1")
        volume_points = Decimal("10") + Decimal("10") * _clamp(excess, Decimal("0"), Decimal("1"))

    # 5) Market: NIFTY regime agreement plus relative-strength magnitude in the trade direction.
    wanted_regime = "BULLISH" if is_long else "BEARISH"
    if regime_name in (None, "INSUFFICIENT_DATA"):
        # The benchmark saying it could not decide is not the benchmark agreeing.
        regime_points = Decimal("0")
    else:
        regime_points = Decimal("10") if regime_name == wanted_regime else Decimal("0")
    relative_value = _number(relative, "relative_strength_percent")
    if relative_value is None:
        rs_points = Decimal("0")
    elif (relative_value > 0) == is_long and relative_value != 0:
        rs_points = Decimal("10") * _clamp(abs(relative_value) / Decimal("0.4"), Decimal("0"), Decimal("1"))
    else:
        rs_points = Decimal("0")

    return {
        "breakout_retest": _round_points(breakout_points),
        "vwap_alignment": _round_points(vwap_points),
        "ema_alignment": _round_points(ema_points),
        "volume_confirmation": _round_points(volume_points),
        "market_confirmation": _round_points(regime_points + rs_points),
    }


def plan_trade(
    side: Side,
    entry: Decimal,
    structural_stop: Decimal,
    indicators: dict,
    controls: dict,
) -> tuple[Decimal, Decimal, int, Decimal] | None:
    """Volatility-aware stop, RR target, and risk- **and** exposure-bounded quantity.

    The caller supplies a raw structural stop price; the stop distance is then the widest of
    that structural distance, an ATR multiple, and a minimum percent of price. Quantity is
    bounded by both the per-trade risk budget and the account's simulated exposure ceiling,
    so a signal is not created only to be discarded later by the risk engine. Shared by every
    strategy so entry/stop/target/quantity mechanics stay identical across them.
    """
    entry = Decimal(str(entry))
    # The stop and target rules live in one module so that a strategy can carry
    # its own and the arithmetic stays in one place. Defaults reproduce exactly
    # what this function used to compute inline.
    plan = exit_plan(
        side=side,
        entry=entry,
        structural_stop=structural_stop,
        atr=_number(indicators, "atr"),
        rules=exit_rules_from(controls),
        account_stop_atr_multiple=controls.get("stop_atr_multiple", 0),
        account_min_stop_percent=controls.get("min_stop_distance_percent", 0),
        minimum_rr=controls["minimum_rr"],
    )
    if plan is None:
        return None
    stop, target, risk_per_unit = plan.stop, plan.target, plan.risk_per_unit

    risk_amount = (
        Decimal(str(controls["account_capital"])) * Decimal(str(controls["risk_per_trade_percent"])) / Decimal("100")
    )
    quantity = int((risk_amount / risk_per_unit).to_integral_value(rounding=ROUND_DOWN))

    leverage = (
        Decimal(str(controls.get("intraday_leverage_multiplier", 1)))
        if controls.get("intraday_leverage_enabled", False)
        else Decimal("1")
    )
    exposure_cap = (
        Decimal(str(controls["account_capital"]))
        * Decimal(str(controls.get("maximum_open_exposure_percent", 100)))
        / Decimal("100")
        * leverage
    )
    if exposure_cap > 0:
        quantity = min(quantity, int((exposure_cap / entry).to_integral_value(rounding=ROUND_DOWN)))

    if quantity < 1:
        return None
    return stop, target, quantity, risk_amount


def _risk_plan(
    side: Side,
    candle: CompletedCandle,
    breakout_level: Decimal,
    indicators: dict,
    controls: dict,
) -> tuple[Decimal, Decimal, int, Decimal] | None:
    """ORB structural stop: the candle extreme, tightened toward the retest level."""
    tolerance = Decimal(str(controls["retest_tolerance_percent"])) / Decimal("100")
    if side == "LONG":
        structural_stop = min(candle.low, breakout_level * (Decimal("1") - tolerance))
    else:
        structural_stop = max(candle.high, breakout_level * (Decimal("1") + tolerance))
    return plan_trade(side, candle.close, structural_stop, indicators, controls)


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
    # Checked after the setup is confirmed and before anything else can refuse
    # it, so that missing data is reported as missing data. A strategy without
    # an input it depends on is not that strategy with a lower score; it is a
    # different one nobody tested. Returning AWAITING rather than holding the
    # breakout state means the setup has to re-form once the data exists,
    # instead of firing the moment a baseline fills in mid-session.
    missing = missing_required(indicators, nifty_indicators, controls.get("required_inputs"))
    if missing:
        return StrategyDecision(
            next_state=AWAITING,
            reason=f"Required market data unavailable: {', '.join(missing)}",
        )
    chop = _chop_refusal(candle, indicators, controls)
    if chop is not None:
        return StrategyDecision(next_state=AWAITING, reason=chop)
    breakdown = _score(side, candle, indicators, nifty_indicators, controls, level)
    score = sum(breakdown.values())
    if score < int(controls["minimum_score"]):
        return StrategyDecision(
            next_state=AWAITING,
            score=score,
            score_breakdown=breakdown,
            reason="Retest failed score threshold",
        )
    plan = _risk_plan(side, candle, level, indicators, controls)
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
