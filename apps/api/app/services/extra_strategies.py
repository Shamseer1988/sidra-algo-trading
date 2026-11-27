"""Additional deterministic, paper-only strategies beyond the opening-range breakout.

Each ``evaluate_*`` function is a pure state machine: given the same completed candle,
indicators, benchmark, controls and prior state it always returns the same decision.
Entry / stop / target / quantity mechanics are delegated to ``paper_strategy.plan_trade``
so risk and exposure behaviour stays identical across strategies.
"""

from decimal import Decimal

from app.services.market_calculations import MARKET_TIMEZONE, CompletedCandle
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
from app.services.signal_inputs import missing_required

VWAP_PULLBACK_VERSION = "vwap-pullback-v1"
EMA_MOMENTUM_VERSION = "ema-momentum-v1"
RS_PULLBACK_VERSION = "rs-pullback-v1"

LONG_PULLBACK = "LONG_PULLBACK"
SHORT_PULLBACK = "SHORT_PULLBACK"
LONG_RS_PULLBACK = "LONG_RS_PULLBACK"
SHORT_RS_PULLBACK = "SHORT_RS_PULLBACK"
DEFAULT_RS_THRESHOLD_PERCENT = Decimal("0.3")


def _required_refusal(indicators: dict, benchmark: dict, controls: dict) -> StrategyDecision | None:
    """Refuse outright when an input this strategy declares it needs is absent.

    ``DEFAULT_REQUIRED_INPUTS`` has named required inputs for all four
    strategies since the gate was built, but ``missing_required`` was only ever
    called from ``evaluate_orb_retest``. The other three declared what they
    could not trade without, the Strategies screen showed it, and nothing
    checked it -- so "rvol" was required of the VWAP pullback and the EMA
    momentum strategy while both happily signalled without it.

    Placed after the setup is confirmed and before scoring, exactly as in the
    opening-range strategy, so that missing data is reported as missing data
    rather than as a low score. Returning AWAITING rather than holding the
    pullback state means the setup has to re-form once the data exists, instead
    of firing the moment a baseline fills in mid-session.
    """
    missing = missing_required(indicators, benchmark, controls.get("required_inputs"))
    if not missing:
        return None
    return StrategyDecision(
        next_state=AWAITING,
        reason=f"Required market data unavailable: {', '.join(missing)}",
    )


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
    """Volume confirmation, where absent volume confirms nothing.

    This awarded full marks for a baseline that did not exist. The same mistake
    was found and fixed in the opening-range scorer in September 2026 and the
    reasoning there applies unchanged: the data-quality gate watches the feed,
    not whether a volume baseline has been computed, so a healthy feed on a
    newly tracked instrument reached here with nothing and collected the cap.

    A VWAP pullback scoring 87 of 100 was measured on 9 October with no volume
    baseline and no benchmark: 20 points of that came from this function and 20
    more from ``_market_points``, which is the whole of the margin over the
    threshold of 80. Without the two freebies the setup scored 47 and would not
    have traded.
    """
    rvol = _relative_volume(indicators)
    threshold = Decimal(str(controls["volume_multiplier"]))
    if rvol is None:
        return Decimal("0")
    if rvol < threshold:
        return Decimal("0")
    excess = (rvol - threshold) / threshold if threshold > 0 else Decimal("1")
    return cap * (Decimal("0.5") + Decimal("0.5") * _clamp(excess, Decimal("0"), Decimal("1")))


def _market_points(side: Side, indicators: dict, benchmark: dict, cap: Decimal) -> Decimal:
    """Benchmark agreement, where a benchmark that said nothing has not agreed.

    Both halves used to pay out on absence: a regime of None or
    INSUFFICIENT_DATA counted as agreement, and a missing relative strength
    took the full scale rather than none of it. Absence must not outscore
    unfavourable evidence -- a benchmark that disagrees earns zero, so one that
    could not be computed cannot earn the cap.
    """
    is_long = side == "LONG"
    wanted = "BULLISH" if is_long else "BEARISH"
    regime = _regime_name(benchmark)
    rs = _relative_strength(indicators)
    half = cap / Decimal("2")
    regime_points = half if regime == wanted else Decimal("0")
    if rs is None or (rs > 0) != is_long or rs == 0:
        rs_points = Decimal("0")
    else:
        rs_points = half * _clamp(abs(rs) / Decimal("0.4"), Decimal("0"), Decimal("1"))
    return regime_points + rs_points


def _trend_points(candle: CompletedCandle, indicators: dict, cap: Decimal) -> Decimal:
    """EMA separation, scored only when there are EMAs to separate.

    ``or Decimal("0")`` collapsed a missing EMA to zero, so both collapsed to
    zero together, the spread between them came out as zero, and the function
    returned half the cap for a trend nobody had measured. Each caller already
    gates on EMA direction before scoring, so the floor is earned where the
    EMAs exist; it is not earned where they do not.
    """
    fast = _number(indicators, "ema_fast")
    slow = _number(indicators, "ema_slow")
    if fast is None or slow is None or candle.close <= 0:
        return Decimal("0")
    spread = abs(fast - slow) / candle.close * Decimal("100")
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

    refusal = _required_refusal(indicators, benchmark, controls)
    if refusal is not None:
        return refusal

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

    refusal = _required_refusal(indicators, benchmark, controls)
    if refusal is not None:
        return refusal

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

    refusal = _required_refusal(indicators, benchmark, controls)
    if refusal is not None:
        return refusal

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
    # Its own copy of the ``_market_points`` bug: None and INSUFFICIENT_DATA
    # both paid the full 20 for a benchmark that had not agreed to anything.
    regime_points = Decimal("20") if regime == wanted else Decimal("0")
    return {
        "relative_strength": _round_points(Decimal("15") + rs_points / Decimal("2"), Decimal("30")),
        "trend_alignment": _round_points(_trend_points(candle, indicators, Decimal("25")), Decimal("25")),
        "pullback_reclaim": _round_points(reclaim, Decimal("25")),
        "market_confirmation": _round_points(regime_points, Decimal("20")),
    }


# --- CPR breakout (G Stock Academy method) -----------------------------------
#
# Long and short (the short side mirrors the notes' buy rules). Built from the
# operator's tutorial notes and money-management sheet, not from evidence: the
# six-month backtest of these rules on 18 November 2026 was not profitable after
# charges. It exists to be paper traded, which is
# why the stored configuration carries live_orders=false.

CPR_BREAKOUT_VERSION = "cpr-breakout-v1"
# Which side has seen a close on the near side of its level after 10:15 AM.
CPR_ARMED = "CPR_ARMED"  # long side
CPR_ARMED_SHORT = "CPR_ARMED_SHORT"
CPR_ARMED_BOTH = "CPR_ARMED_BOTH"
CPR_STATES = {AWAITING, CPR_ARMED, CPR_ARMED_SHORT, CPR_ARMED_BOTH}
# The first hour must be complete before a breakout of its high means anything,
# and the notes forbid new trades after 2:45 PM. Fixed rather than read from the
# account's trade window, which belongs to the live strategy (9:45-11:30 AM).
CPR_START = "10:15"
CPR_CUTOFF = "14:45"
DEFAULT_CPR_NARROW_PERCENT = Decimal("0.35")
# The money-management sheet's stop: 0.5% below entry. Handed to plan_trade as
# the structural level; the stored exit rules keep the floors from widening it.
CPR_STOP_PERCENT = Decimal("0.5")


def _cpr_window(candle: CompletedCandle) -> bool:
    opened = candle.opened_at.astimezone(MARKET_TIMEZONE).strftime("%H:%M")
    closed = candle.closed_at.astimezone(MARKET_TIMEZONE).strftime("%H:%M")
    return CPR_START <= opened and closed <= CPR_CUTOFF


def _level(values: object, key: str) -> Decimal | None:
    if not isinstance(values, dict):
        return None
    return _number(values, key)


def cpr_relation(today: dict, yesterday: dict) -> str | None:
    """How today's Central Pivot Range sits against yesterday's.

    HIGHER: entirely above. OVERLAP_HIGHER: top and bottom both higher, ranges
    touching. INSIDE: within yesterday's. LOWER / OVERLAP_LOWER mirror the
    first two. OUTSIDE: wider on both sides. None when a level is missing.
    """
    t_tc, t_bc = _level(today, "tc"), _level(today, "bc")
    y_tc, y_bc = _level(yesterday, "tc"), _level(yesterday, "bc")
    if t_tc is None or t_bc is None or y_tc is None or y_bc is None:
        return None
    if t_bc > y_tc:
        return "HIGHER"
    if t_tc < y_bc:
        return "LOWER"
    if t_tc > y_tc and t_bc > y_bc:
        return "OVERLAP_HIGHER"
    if t_tc < y_tc and t_bc < y_bc:
        return "OVERLAP_LOWER"
    if t_tc <= y_tc and t_bc >= y_bc:
        return "INSIDE"
    return "OUTSIDE"


def cpr_buy_setup(indicators: dict, narrow_percent: Decimal = DEFAULT_CPR_NARROW_PERCENT) -> tuple[bool, str, dict]:
    """Whether today's CPR conditions allow a long, why not, and the levels used.

    The rules, in the order the notes give them:
      1. Two-day relation is HIGHER, OVERLAP_HIGHER or INSIDE.
      2. Yesterday closed below its R1 and its high stayed below its R2.
      3. Today's CPR is narrow and its top is below yesterday's R1.
      4. The first hour's high is above yesterday's high and below today's R2.
    """
    cpr = indicators.get("cpr") if isinstance(indicators, dict) else None
    first_hour = indicators.get("first_hour") if isinstance(indicators, dict) else None
    if not isinstance(cpr, dict) or not cpr.get("today") or not cpr.get("yesterday") or not cpr.get("prior_day"):
        return False, "CPR levels are not available (needs two prior daily candles)", {}
    if not isinstance(first_hour, dict) or not first_hour.get("complete") or first_hour.get("high") is None:
        return False, "First hour (9:15-10:15 AM) is not complete", {}
    today, yesterday, prior = cpr["today"], cpr["yesterday"], cpr["prior_day"]
    fh_high = _level(first_hour, "high")
    y_high, y_close = _level(prior, "high"), _level(prior, "close")
    y_r1, y_r2 = _level(yesterday, "r1"), _level(yesterday, "r2")
    t_tc, t_r2 = _level(today, "tc"), _level(today, "r2")
    relation = cpr_relation(today, yesterday)
    width = _level(today, "width_percent")
    levels = {"relation": relation, "width_percent": width, "first_hour_high": fh_high}
    if None in (fh_high, y_high, y_close, y_r1, y_r2, t_tc, t_r2, width):
        return False, "CPR levels are incomplete", levels
    if relation not in {"HIGHER", "OVERLAP_HIGHER", "INSIDE"}:
        return False, f"CPR relation is {relation}, not higher, overlapping-higher or inside", levels
    if not y_close < y_r1:
        return False, "Yesterday closed at or above its R1", levels
    if not y_high < y_r2:
        return False, "Yesterday's high reached its R2", levels
    if width >= narrow_percent:
        return False, f"Today's CPR is not narrow ({width}% is not below {narrow_percent}%)", levels
    if not t_tc < y_r1:
        return False, "Today's CPR is not below yesterday's R1", levels
    if not fh_high > y_high:
        return False, "First-hour high did not clear yesterday's high", levels
    if not fh_high < t_r2:
        return False, "First-hour high is already at or above today's R2", levels
    return True, "CPR buy conditions met", levels


def cpr_sell_setup(indicators: dict, narrow_percent: Decimal = DEFAULT_CPR_NARROW_PERCENT) -> tuple[bool, str, dict]:
    """The mirror image of ``cpr_buy_setup``, for a short.

    The operator's notes give buy rules only; these are their reflection:
      1. Two-day relation is LOWER, OVERLAP_LOWER or INSIDE.
      2. Yesterday closed above its S1 and its low stayed above its S2.
      3. Today's CPR is narrow and its bottom is above yesterday's S1.
      4. The first hour's low is below yesterday's low and above today's S2.
    The notes' own line -- a lower CPR is bearish only while price is below
    today's CPR -- is applied at entry, where the price is known.
    """
    cpr = indicators.get("cpr") if isinstance(indicators, dict) else None
    first_hour = indicators.get("first_hour") if isinstance(indicators, dict) else None
    if not isinstance(cpr, dict) or not cpr.get("today") or not cpr.get("yesterday") or not cpr.get("prior_day"):
        return False, "CPR levels are not available (needs two prior daily candles)", {}
    if not isinstance(first_hour, dict) or not first_hour.get("complete") or first_hour.get("low") is None:
        return False, "First hour (9:15-10:15 AM) is not complete", {}
    today, yesterday, prior = cpr["today"], cpr["yesterday"], cpr["prior_day"]
    fh_low = _level(first_hour, "low")
    y_low, y_close = _level(prior, "low"), _level(prior, "close")
    y_s1, y_s2 = _level(yesterday, "s1"), _level(yesterday, "s2")
    t_bc, t_s2 = _level(today, "bc"), _level(today, "s2")
    relation = cpr_relation(today, yesterday)
    width = _level(today, "width_percent")
    levels = {"relation": relation, "width_percent": width, "first_hour_low": fh_low, "today_bc": t_bc}
    if None in (fh_low, y_low, y_close, y_s1, y_s2, t_bc, t_s2, width):
        return False, "CPR levels are incomplete", levels
    if relation not in {"LOWER", "OVERLAP_LOWER", "INSIDE"}:
        return False, f"CPR relation is {relation}, not lower, overlapping-lower or inside", levels
    if not y_close > y_s1:
        return False, "Yesterday closed at or below its S1", levels
    if not y_low > y_s2:
        return False, "Yesterday's low reached its S2", levels
    if width >= narrow_percent:
        return False, f"Today's CPR is not narrow ({width}% is not below {narrow_percent}%)", levels
    if not t_bc > y_s1:
        return False, "Today's CPR is not above yesterday's S1", levels
    if not fh_low < y_low:
        return False, "First-hour low did not break yesterday's low", levels
    if not fh_low > t_s2:
        return False, "First-hour low is already at or below today's S2", levels
    return True, "CPR sell conditions met", levels


def evaluate_cpr_breakout(
    candle: CompletedCandle,
    indicators: dict,
    benchmark: dict,
    controls: dict,
    prior_state: str = AWAITING,
) -> StrategyDecision:
    """Buy a close above the first-hour high, or sell a close below its low.

    The break has to happen after 10:15 AM. A candle that closes on the near
    side of a level arms that side, and the signal is a later close through it
    -- or a candle that opens on the near side and closes through. A share
    already beyond the level all afternoon never arms and is not chased. A
    short also needs the close below today's CPR, per the notes.
    """
    if not _cpr_window(candle):
        return StrategyDecision(next_state=AWAITING, reason="Outside the CPR window (10:15 AM - 2:45 PM)")
    if prior_state == SIGNALLED:
        return StrategyDecision(next_state=SIGNALLED, reason="Instrument already signalled this session")
    if prior_state not in CPR_STATES:
        return StrategyDecision(next_state=AWAITING, reason="Unknown strategy state was reset")

    narrow_raw = controls.get("cpr_narrow_percent")
    narrow = Decimal(str(narrow_raw)) if narrow_raw is not None else DEFAULT_CPR_NARROW_PERCENT
    long_ok, long_why, long_levels = cpr_buy_setup(indicators, narrow)
    short_ok, short_why, short_levels = cpr_sell_setup(indicators, narrow)
    if not long_ok and not short_ok:
        return StrategyDecision(next_state=AWAITING, reason=f"Buy: {long_why}. Sell: {short_why}")

    armed_long = prior_state in {CPR_ARMED, CPR_ARMED_BOTH}
    armed_short = prior_state in {CPR_ARMED_SHORT, CPR_ARMED_BOTH}
    side: Side | None = None
    levels: dict = {}
    if long_ok:
        high = long_levels["first_hour_high"]
        if candle.close > high and (candle.open <= high or armed_long):
            side, levels = "LONG", long_levels
    if side is None and short_ok:
        low = short_levels["first_hour_low"]
        below_cpr = candle.close < short_levels["today_bc"]
        if candle.close < low and below_cpr and (candle.open >= low or armed_short):
            side, levels = "SHORT", short_levels

    if side is None:
        armed_long = armed_long or (long_ok and candle.close <= long_levels["first_hour_high"])
        armed_short = armed_short or (short_ok and candle.close >= short_levels["first_hour_low"])
        state = (
            CPR_ARMED_BOTH
            if armed_long and armed_short
            else CPR_ARMED
            if armed_long
            else CPR_ARMED_SHORT
            if armed_short
            else AWAITING
        )
        return StrategyDecision(
            next_state=state, reason="Waiting for a close through the first-hour range; not chasing"
        )

    refusal = _required_refusal(indicators, benchmark, controls)
    if refusal is not None:
        return refusal
    breakdown = _score_cpr_breakout(side, levels, benchmark)
    score = sum(breakdown.values())
    if score < int(controls["minimum_score"]):
        return StrategyDecision(
            next_state=AWAITING, score=score, score_breakdown=breakdown, reason="Breakout failed score threshold"
        )
    offset = candle.close * CPR_STOP_PERCENT / Decimal("100")
    structural_stop = candle.close - offset if side == "LONG" else candle.close + offset
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


def _score_cpr_breakout(side: Side, levels: dict, benchmark: dict) -> dict[str, int]:
    """Informational only: every rule has already passed when this runs.

    Kept so the signal carries a breakdown like every other strategy. The
    stored configuration sets minimum_score to 0, so this never filters.
    """
    if side == "LONG":
        relation_points = {"HIGHER": 30, "OVERLAP_HIGHER": 25, "INSIDE": 20}.get(levels.get("relation"), 0)
    else:
        relation_points = {"LOWER": 30, "OVERLAP_LOWER": 25, "INSIDE": 20}.get(levels.get("relation"), 0)
    width = Decimal(str(levels.get("width_percent") or DEFAULT_CPR_NARROW_PERCENT))
    narrow = Decimal("30") * _clamp(
        (DEFAULT_CPR_NARROW_PERCENT - width) / DEFAULT_CPR_NARROW_PERCENT, Decimal("0"), Decimal("1")
    )
    wanted = "BULLISH" if side == "LONG" else "BEARISH"
    return {
        "cpr_relation": relation_points,
        "cpr_narrowness": _round_points(narrow, Decimal("30")),
        "first_hour_breakout": 20,
        "market_confirmation": 20 if _regime_name(benchmark) == wanted else 0,
    }


# --- Stocks-in-Play momentum ---------------------------------------------------
#
# One look per share per day, at 10:10 AM. A share that has already traded far
# more than usual this morning, has moved at least 1% from yesterday's close,
# and sits on that side of VWAP, is entered in the direction of the move and
# held to the square-off, with a stop half a daily ATR away and no target.
#
# From the 27 November 2026 research replay (59 shares, 148 sessions,
# charges included): positive in both the first 60% of sessions and the last
# 40%, and across most settings at 10:05, 10:10 and 10:15 AM, but not at
# 10:00 or after 10:20, with two losing months of eight and no t-statistic
# above 2. That is a reason to paper trade it, not to trust it.

SIP_MOMENTUM_VERSION = "sip-momentum-v1"
SIP_DONE = "SIP_DONE"
SIP_STATES = {AWAITING, SIP_DONE}
# The decision candle is the one that spans this minute: for 1-minute candles,
# 10:09-10:10, so the trade is entered at about 10:10.
SIP_DECISION_TIME = "10:10"
DEFAULT_SIP_MIN_SESSION_RVOL = Decimal("2.5")
DEFAULT_SIP_MIN_MOVE_PERCENT = Decimal("1.0")
DEFAULT_SIP_STOP_DAILY_ATR = Decimal("0.5")


def _hhmm(moment) -> str:
    return moment.astimezone(MARKET_TIMEZONE).strftime("%H:%M")


def _control(controls: dict, key: str, default: Decimal) -> Decimal:
    raw = controls.get(key)
    return Decimal(str(raw)) if raw is not None else default


def sip_rank(session_rvol: Decimal, move_percent: Decimal) -> Decimal:
    """How strongly a share is in play: unusual volume times the size of its move.

    The research ranked candidates by this; the signal records it so the paper
    record can later be cut to the top one or top three of each day.
    """
    return (session_rvol * abs(move_percent)).quantize(Decimal("0.01"))


def evaluate_sip_momentum(
    candle: CompletedCandle,
    indicators: dict,
    benchmark: dict,
    controls: dict,
    prior_state: str = AWAITING,
) -> StrategyDecision:
    """Decide once, on the candle spanning 10:10 AM, and never again that day."""
    if prior_state == SIGNALLED:
        return StrategyDecision(next_state=SIGNALLED, reason="Instrument already signalled this session")
    if prior_state not in SIP_STATES:
        return StrategyDecision(next_state=AWAITING, reason="Unknown strategy state was reset")
    if prior_state == SIP_DONE:
        return StrategyDecision(next_state=SIP_DONE, reason="Today's 10:10 AM decision has been made")
    opened, closed = _hhmm(candle.opened_at), _hhmm(candle.closed_at)
    if closed < SIP_DECISION_TIME:
        return StrategyDecision(next_state=AWAITING, reason="Waiting for the 10:10 AM decision")
    if opened >= SIP_DECISION_TIME:
        return StrategyDecision(next_state=SIP_DONE, reason="No candle spanned 10:10 AM; no trade today")

    rvol = _number(indicators, "session_rvol")
    daily_atr = _number(indicators, "daily_atr")
    vwap = _number(indicators, "vwap")
    cpr = indicators.get("cpr") if isinstance(indicators.get("cpr"), dict) else {}
    prior_close = _level(cpr.get("prior_day"), "close")
    missing = [
        name
        for name, value in (
            ("session volume baseline", rvol),
            ("daily ATR", daily_atr),
            ("VWAP", vwap),
            ("yesterday's close", prior_close),
        )
        if value is None or value <= 0
    ]
    if missing:
        return StrategyDecision(next_state=SIP_DONE, reason=f"Not available at 10:10 AM: {', '.join(missing)}")

    min_rvol = _control(controls, "sip_min_session_rvol", DEFAULT_SIP_MIN_SESSION_RVOL)
    min_move = _control(controls, "sip_min_move_percent", DEFAULT_SIP_MIN_MOVE_PERCENT)
    move = (candle.close - prior_close) / prior_close * Decimal("100")
    facts = f"volume {rvol:.2f}x usual, move {move:+.2f}% from yesterday's close"
    if rvol < min_rvol:
        return StrategyDecision(next_state=SIP_DONE, reason=f"Not in play: {facts}; needs {min_rvol}x")
    if abs(move) < min_move:
        return StrategyDecision(next_state=SIP_DONE, reason=f"Move too small: {facts}; needs {min_move}%")
    side: Side = "LONG" if move > 0 else "SHORT"
    if (side == "LONG" and candle.close <= vwap) or (side == "SHORT" and candle.close >= vwap):
        return StrategyDecision(next_state=SIP_DONE, reason=f"Price is on the wrong side of VWAP: {facts}")

    refusal = _required_refusal(indicators, benchmark, controls)
    if refusal is not None:
        return StrategyDecision(next_state=SIP_DONE, reason=refusal.reason)

    rank = sip_rank(rvol, move)
    breakdown = {
        "session_volume": _round_points(_clamp(rvol, Decimal("0"), Decimal("10")) * Decimal("5"), Decimal("50")),
        "move_from_close": _round_points(_clamp(abs(move), Decimal("0"), Decimal("5")) * Decimal("10"), Decimal("50")),
    }
    score = sum(breakdown.values())
    if score < int(controls["minimum_score"]):
        return StrategyDecision(
            next_state=SIP_DONE, score=score, score_breakdown=breakdown, reason=f"Below score threshold: {facts}"
        )
    offset = daily_atr * _control(controls, "sip_stop_daily_atr", DEFAULT_SIP_STOP_DAILY_ATR)
    structural_stop = candle.close - offset if side == "LONG" else candle.close + offset
    plan = plan_trade(side, candle.close, structural_stop, indicators, controls)
    if plan is None:
        return StrategyDecision(
            next_state=SIP_DONE, score=score, score_breakdown=breakdown, reason="Risk plan is invalid"
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
        reason=f"Paper signal confirmed: {facts}, rank {rank}",
    )
