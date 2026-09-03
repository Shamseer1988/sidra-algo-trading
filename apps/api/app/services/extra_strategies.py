"""Additional deterministic, paper-only strategies beyond the opening-range breakout.

Each ``evaluate_*`` function is a pure state machine: given the same completed candle,
indicators, benchmark, controls and prior state it always returns the same decision.
Entry / stop / target / quantity mechanics are delegated to ``paper_strategy.plan_trade``
so risk and exposure behaviour stays identical across strategies.
"""

from decimal import Decimal

from app.services.market_calculations import CompletedCandle
from app.services.paper_strategy import (
    AWAITING,
    SIGNALLED,
    Side,
    StrategyDecision,
    _clamp,
    _inside_trade_window,
    _number,
    _round_points,
    plan_trade,
)

VWAP_PULLBACK_VERSION = "vwap-pullback-v1"
EMA_MOMENTUM_VERSION = "ema-momentum-v1"
RS_PULLBACK_VERSION = "rs-pullback-v1"

LONG_PULLBACK = "LONG_PULLBACK"
SHORT_PULLBACK = "SHORT_PULLBACK"
LONG_RS_PULLBACK = "LONG_RS_PULLBACK"
SHORT_RS_PULLBACK = "SHORT_RS_PULLBACK"
DEFAULT_RS_THRESHOLD_PERCENT = Decimal("0.3")


def _regime_name(benchmark: dict) -> str | None:
    regime = benchmark.get("nifty_regime") if isinstance(benchmark.get("nifty_regime"), dict) else {}
    return regime.get("regime")


def _relative_strength(indicators: dict) -> Decimal | None:
    relative = indicators.get("relative_strength") if isinstance(indicators.get("relative_strength"), dict) else {}
    return _number(relative, "relative_strength_percent")


def _relative_volume(indicators: dict) -> Decimal | None:
    volume = indicators.get("volume") if isinstance(indicators.get("volume"), dict) else {}
    return _number(volume, "relative_volume")


def _volume_points(indicators: dict, controls: dict, cap: Decimal) -> Decimal:
    """Missing volume data awards full credit; below the RVOL multiple is a hard zero."""
    rvol = _relative_volume(indicators)
    threshold = Decimal(str(controls["volume_multiplier"]))
    if rvol is None:
        return cap
    if rvol < threshold:
        return Decimal("0")
    excess = (rvol - threshold) / threshold if threshold > 0 else Decimal("1")
    return cap * (Decimal("0.5") + Decimal("0.5") * _clamp(excess, Decimal("0"), Decimal("1")))


def _market_points(side: Side, indicators: dict, benchmark: dict, cap: Decimal) -> Decimal:
    is_long = side == "LONG"
    wanted = "BULLISH" if is_long else "BEARISH"
    regime = _regime_name(benchmark)
    regime_ok = regime in (None, "INSUFFICIENT_DATA") or regime == wanted
    rs = _relative_strength(indicators)
    rs_ok = rs is None or ((rs > 0) == is_long)
    rs_scale = _clamp(abs(rs) / Decimal("0.4"), Decimal("0"), Decimal("1")) if rs is not None else Decimal("1")
    half = cap / Decimal("2")
    return (half if regime_ok else Decimal("0")) + (half * rs_scale if rs_ok else Decimal("0"))


def _trend_points(candle: CompletedCandle, indicators: dict, cap: Decimal) -> Decimal:
    fast = _number(indicators, "ema_fast") or Decimal("0")
    slow = _number(indicators, "ema_slow") or Decimal("0")
    spread = abs(fast - slow) / candle.close * Decimal("100") if candle.close > 0 else Decimal("0")
    base = cap / Decimal("2")
    return base + base * _clamp(spread / Decimal("0.3"), Decimal("0"), Decimal("1"))


# --------------------------------------------------------------------------------------
# VWAP pullback: trade a controlled pullback to session VWAP inside an EMA-defined trend.
# --------------------------------------------------------------------------------------


def evaluate_vwap_pullback(
    candle: CompletedCandle,
    indicators: dict,
    benchmark: dict,
    controls: dict,
    prior_state: str = AWAITING,
) -> StrategyDecision:
    if not _inside_trade_window(candle, controls):
        return StrategyDecision(next_state=AWAITING, reason="Outside configured trade window")

    vwap = _number(indicators, "vwap")
    fast = _number(indicators, "ema_fast")
    slow = _number(indicators, "ema_slow")
    atr = _number(indicators, "atr")
    if vwap is None or fast is None or slow is None or atr is None or atr <= 0:
        return StrategyDecision(next_state=AWAITING, reason="Trend and VWAP indicators are not ready")
    if prior_state == SIGNALLED:
        return StrategyDecision(next_state=SIGNALLED, reason="Instrument already signalled this session")
    if prior_state not in {AWAITING, LONG_PULLBACK, SHORT_PULLBACK}:
        return StrategyDecision(next_state=AWAITING, reason="Unknown strategy state was reset")

    tolerance = Decimal(str(controls["retest_tolerance_percent"])) / Decimal("100")
    uptrend = fast > slow and candle.close > slow
    downtrend = fast < slow and candle.close < slow

    if prior_state == AWAITING:
        if uptrend and candle.low <= vwap * (Decimal("1") + tolerance):
            return StrategyDecision(next_state=LONG_PULLBACK, reason="Pullback into VWAP within an uptrend")
        if downtrend and candle.high >= vwap * (Decimal("1") - tolerance):
            return StrategyDecision(next_state=SHORT_PULLBACK, reason="Rally into VWAP within a downtrend")
        return StrategyDecision(next_state=AWAITING, reason="Awaiting a pullback to VWAP")

    side: Side = "LONG" if prior_state == LONG_PULLBACK else "SHORT"
    aligned = uptrend if side == "LONG" else downtrend
    if not aligned:
        return StrategyDecision(next_state=AWAITING, reason="Trend alignment was lost")
    broke = candle.close < vwap - atr if side == "LONG" else candle.close > vwap + atr
    if broke:
        return StrategyDecision(next_state=AWAITING, reason="Price broke through VWAP against the trend")
    reclaimed = (
        candle.close > vwap and candle.close > candle.open
        if side == "LONG"
        else candle.close < vwap and candle.close < candle.open
    )
    if not reclaimed:
        return StrategyDecision(next_state=prior_state, reason="Awaiting the VWAP reclaim candle")

    breakdown = _score_vwap_pullback(side, candle, indicators, benchmark, controls, vwap, atr)
    score = sum(breakdown.values())
    if score < int(controls["minimum_score"]):
        return StrategyDecision(
            next_state=AWAITING, score=score, score_breakdown=breakdown, reason="Reclaim failed score threshold"
        )
    structural_stop = (
        min(candle.low, vwap - atr * Decimal("0.25"))
        if side == "LONG"
        else max(candle.high, vwap + atr * Decimal("0.25"))
    )
    plan = plan_trade(side, candle.close, structural_stop, indicators, controls)
    if plan is None:
        return StrategyDecision(
            next_state=AWAITING, score=score, score_breakdown=breakdown, reason="Risk plan is invalid"
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


def _score_vwap_pullback(
    side: Side,
    candle: CompletedCandle,
    indicators: dict,
    benchmark: dict,
    controls: dict,
    vwap: Decimal,
    atr: Decimal,
) -> dict[str, int]:
    is_long = side == "LONG"
    # Reward a firm reclaim of VWAP that closes near it rather than already extended.
    reclaim_distance = (candle.close - vwap) / atr if is_long else (vwap - candle.close) / atr
    reclaim = Decimal("30") * _clamp(
        Decimal("1") - abs(reclaim_distance - Decimal("0.4")) / Decimal("0.9"), Decimal("0"), Decimal("1")
    )
    return {
        "trend_alignment": _round_points(_trend_points(candle, indicators, Decimal("30")), Decimal("30")),
        "vwap_reclaim": _round_points(reclaim, Decimal("30")),
        "volume_confirmation": _round_points(_volume_points(indicators, controls, Decimal("20")), Decimal("20")),
        "market_confirmation": _round_points(_market_points(side, indicators, benchmark, Decimal("20")), Decimal("20")),
    }


# --------------------------------------------------------------------------------------
# EMA momentum: trade a fresh push in the direction of a stacked EMA / VWAP trend.
# --------------------------------------------------------------------------------------


def evaluate_ema_momentum(
    candle: CompletedCandle,
    indicators: dict,
    benchmark: dict,
    controls: dict,
    prior_state: str = AWAITING,
) -> StrategyDecision:
    if not _inside_trade_window(candle, controls):
        return StrategyDecision(next_state=AWAITING, reason="Outside configured trade window")

    vwap = _number(indicators, "vwap")
    fast = _number(indicators, "ema_fast")
    slow = _number(indicators, "ema_slow")
    atr = _number(indicators, "atr")
    opening = indicators.get("opening_range") if isinstance(indicators.get("opening_range"), dict) else {}
    or_high = _number(opening, "high")
    or_low = _number(opening, "low")
    if vwap is None or fast is None or slow is None or atr is None or atr <= 0:
        return StrategyDecision(next_state=AWAITING, reason="Trend indicators are not ready")
    if not opening.get("complete") or or_high is None or or_low is None:
        return StrategyDecision(next_state=AWAITING, reason="Opening range is incomplete")
    if prior_state == SIGNALLED:
        return StrategyDecision(next_state=SIGNALLED, reason="Instrument already signalled this session")

    extension = _number(indicators, "extension_atr")
    long_stack = candle.close > fast > slow and candle.close > vwap
    short_stack = candle.close < fast < slow and candle.close < vwap
    up_push = long_stack and candle.close > or_high and candle.close > candle.open
    down_push = short_stack and candle.close < or_low and candle.close < candle.open
    overextended = extension is not None and extension > Decimal("2.0")

    if not (up_push or down_push):
        return StrategyDecision(next_state=AWAITING, reason="Awaiting a momentum push through the opening range")
    if overextended:
        return StrategyDecision(next_state=AWAITING, reason="Price is already extended from VWAP")

    side: Side = "LONG" if up_push else "SHORT"
    level = or_high if side == "LONG" else or_low
    breakdown = _score_ema_momentum(side, candle, indicators, benchmark, controls, vwap, fast, atr, level)
    score = sum(breakdown.values())
    if score < int(controls["minimum_score"]):
        return StrategyDecision(
            next_state=AWAITING, score=score, score_breakdown=breakdown, reason="Push failed score threshold"
        )
    structural_stop = (
        min(candle.low, fast, candle.close - atr) if side == "LONG" else max(candle.high, fast, candle.close + atr)
    )
    plan = plan_trade(side, candle.close, structural_stop, indicators, controls)
    if plan is None:
        return StrategyDecision(
            next_state=AWAITING, score=score, score_breakdown=breakdown, reason="Risk plan is invalid"
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


def _score_ema_momentum(
    side: Side,
    candle: CompletedCandle,
    indicators: dict,
    benchmark: dict,
    controls: dict,
    vwap: Decimal,
    fast: Decimal,
    atr: Decimal,
    level: Decimal,
) -> dict[str, int]:
    is_long = side == "LONG"
    stack_distance = (candle.close - fast) / atr if is_long else (fast - candle.close) / atr
    stack = Decimal("15") + Decimal("15") * _clamp(stack_distance / Decimal("0.5"), Decimal("0"), Decimal("1"))
    vwap_distance = (candle.close - vwap) / atr if is_long else (vwap - candle.close) / atr
    vwap_points = Decimal("20") * _clamp(
        Decimal("1") - abs(vwap_distance - Decimal("0.75")) / Decimal("1.25"), Decimal("0"), Decimal("1")
    )
    breakout_distance = (candle.close - level) / atr if is_long else (level - candle.close) / atr
    breakout = Decimal("20") * _clamp(breakout_distance / Decimal("0.35"), Decimal("0"), Decimal("1"))
    return {
        "ema_stack": _round_points(stack, Decimal("30")),
        "vwap_position": _round_points(vwap_points, Decimal("20")),
        "breakout": _round_points(breakout, Decimal("20")),
        "volume_confirmation": _round_points(_volume_points(indicators, controls, Decimal("15")), Decimal("15")),
        "market_confirmation": _round_points(_market_points(side, indicators, benchmark, Decimal("15")), Decimal("15")),
    }


# --------------------------------------------------------------------------------------
# Relative-strength pullback: a leader that is outperforming NIFTY, pulling back to its
# fast EMA and resuming in the direction of its relative strength.
# --------------------------------------------------------------------------------------


def _rs_threshold(controls: dict) -> Decimal:
    raw = controls.get("rs_threshold_percent")
    return Decimal(str(raw)) if raw is not None else DEFAULT_RS_THRESHOLD_PERCENT


def evaluate_rs_pullback(
    candle: CompletedCandle,
    indicators: dict,
    benchmark: dict,
    controls: dict,
    prior_state: str = AWAITING,
) -> StrategyDecision:
    if not _inside_trade_window(candle, controls):
        return StrategyDecision(next_state=AWAITING, reason="Outside configured trade window")

    fast = _number(indicators, "ema_fast")
    slow = _number(indicators, "ema_slow")
    atr = _number(indicators, "atr")
    rs = _relative_strength(indicators)
    if fast is None or slow is None or atr is None or atr <= 0:
        return StrategyDecision(next_state=AWAITING, reason="Trend indicators are not ready")
    if rs is None:
        return StrategyDecision(next_state=AWAITING, reason="Relative strength is not available")
    if prior_state == SIGNALLED:
        return StrategyDecision(next_state=SIGNALLED, reason="Instrument already signalled this session")
    if prior_state not in {AWAITING, LONG_RS_PULLBACK, SHORT_RS_PULLBACK}:
        return StrategyDecision(next_state=AWAITING, reason="Unknown strategy state was reset")

    threshold = _rs_threshold(controls)
    tolerance = Decimal(str(controls["retest_tolerance_percent"])) / Decimal("100")
    long_leader = fast > slow and candle.close > slow and rs >= threshold
    short_laggard = fast < slow and candle.close < slow and rs <= -threshold

    if prior_state == AWAITING:
        if long_leader and candle.low <= fast * (Decimal("1") + tolerance):
            return StrategyDecision(next_state=LONG_RS_PULLBACK, reason="Leader pulling back to its fast EMA")
        if short_laggard and candle.high >= fast * (Decimal("1") - tolerance):
            return StrategyDecision(next_state=SHORT_RS_PULLBACK, reason="Laggard rallying to its fast EMA")
        return StrategyDecision(next_state=AWAITING, reason="Awaiting a relative-strength pullback")

    side: Side = "LONG" if prior_state == LONG_RS_PULLBACK else "SHORT"
    still_leading = (rs >= threshold / 2) if side == "LONG" else (rs <= -threshold / 2)
    aligned = (fast > slow and candle.close >= slow) if side == "LONG" else (fast < slow and candle.close <= slow)
    if not (still_leading and aligned):
        return StrategyDecision(next_state=AWAITING, reason="Trend or relative-strength lead was lost")
    reclaimed = (
        candle.close > fast and candle.close > candle.open
        if side == "LONG"
        else candle.close < fast and candle.close < candle.open
    )
    if not reclaimed:
        return StrategyDecision(next_state=prior_state, reason="Awaiting the fast-EMA reclaim candle")

    breakdown = _score_rs_pullback(side, candle, indicators, benchmark, rs, threshold, fast, atr)
    score = sum(breakdown.values())
    if score < int(controls["minimum_score"]):
        return StrategyDecision(
            next_state=AWAITING, score=score, score_breakdown=breakdown, reason="Reclaim failed score threshold"
        )
    structural_stop = (
        min(candle.low, fast - atr * Decimal("0.25"))
        if side == "LONG"
        else max(candle.high, fast + atr * Decimal("0.25"))
    )
    plan = plan_trade(side, candle.close, structural_stop, indicators, controls)
    if plan is None:
        return StrategyDecision(
            next_state=AWAITING, score=score, score_breakdown=breakdown, reason="Risk plan is invalid"
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


def _score_rs_pullback(
    side: Side,
    candle: CompletedCandle,
    indicators: dict,
    benchmark: dict,
    rs: Decimal,
    threshold: Decimal,
    fast: Decimal,
    atr: Decimal,
) -> dict[str, int]:
    is_long = side == "LONG"
    span = threshold * Decimal("2") if threshold > 0 else Decimal("0.6")
    rs_points = Decimal("30") * _clamp((abs(rs) - threshold) / span, Decimal("0"), Decimal("1"))
    reclaim_distance = (candle.close - fast) / atr if is_long else (fast - candle.close) / atr
    reclaim = Decimal("25") * _clamp(reclaim_distance / Decimal("0.3"), Decimal("0"), Decimal("1"))
    regime = _regime_name(benchmark)
    wanted = "BULLISH" if is_long else "BEARISH"
    regime_points = Decimal("20") if regime in (None, "INSUFFICIENT_DATA", wanted) else Decimal("0")
    return {
        "relative_strength": _round_points(Decimal("15") + rs_points / Decimal("2"), Decimal("30")),
        "trend_alignment": _round_points(_trend_points(candle, indicators, Decimal("25")), Decimal("25")),
        "pullback_reclaim": _round_points(reclaim, Decimal("25")),
        "market_confirmation": _round_points(regime_points, Decimal("20")),
    }
