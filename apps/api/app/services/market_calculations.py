"""Pure, completed-candle-only intraday calculations.

The functions in this module have no database or broker dependency.  They are kept
deterministic so replay and strategy evaluation can use exactly the same math.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.services.trading_calendar import (
    DEFAULT_TRADING_CALENDAR,
    MARKET_TIMEZONE,
    TradingCalendar,
)
from app.services.trading_calendar import (
    REGULAR_CLOSE as MARKET_CLOSE,
)
from app.services.trading_calendar import (
    REGULAR_OPEN as MARKET_OPEN,
)


def is_regular_market_timestamp(timestamp: datetime, calendar: TradingCalendar | None = None) -> bool:
    """Return true only when a confirmed exchange session is open."""
    return (calendar or DEFAULT_TRADING_CALENDAR).is_open(timestamp)


@dataclass(frozen=True)
class CompletedCandle:
    instrument_token: str
    timeframe_seconds: int
    opened_at: datetime
    closed_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    tick_count: int

    @property
    def session_date(self) -> date:
        return self.opened_at.astimezone(MARKET_TIMEZONE).date()


def _decimal(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.0001")))


def session_candles(candles: list[CompletedCandle], session: date | None = None) -> list[CompletedCandle]:
    """Return one ordered India-market session; incomplete candles cannot enter."""
    if not candles:
        return []
    resolved_session = session or candles[-1].session_date
    return sorted(
        (
            candle
            for candle in candles
            if candle.session_date == resolved_session
            and is_regular_market_timestamp(candle.opened_at)
            and candle.closed_at.astimezone(MARKET_TIMEZONE).time() <= MARKET_CLOSE
        ),
        key=lambda candle: candle.opened_at,
    )


def ema(candles: list[CompletedCandle], period: int) -> Decimal | None:
    if period < 2 or len(candles) < period:
        return None
    multiplier = Decimal("2") / Decimal(period + 1)
    value = candles[0].close
    for candle in candles[1:]:
        value = ((candle.close - value) * multiplier) + value
    return value


def session_vwap(candles: list[CompletedCandle]) -> Decimal | None:
    numerator = Decimal("0")
    denominator = 0
    for candle in candles:
        if candle.volume <= 0:
            continue
        typical_price = (candle.high + candle.low + candle.close) / Decimal("3")
        numerator += typical_price * candle.volume
        denominator += candle.volume
    return numerator / denominator if denominator else None


def volume_metrics(candles: list[CompletedCandle], lookback: int) -> dict[str, float | int | None]:
    if not candles:
        return {"current_volume": None, "average_volume": None, "relative_volume": None}
    current = candles[-1].volume
    prior = candles[max(0, len(candles) - 1 - lookback) : -1]
    average = (sum(candle.volume for candle in prior) / len(prior)) if prior else None
    return {
        "current_volume": current,
        "average_volume": round(average, 4) if average is not None else None,
        "relative_volume": round(current / average, 4) if average else None,
    }


def true_ranges(candles: list[CompletedCandle]) -> list[Decimal]:
    """Per-candle true range. The first bar has no prior close, so it uses high-low only."""
    ranges: list[Decimal] = []
    prior_close: Decimal | None = None
    for candle in candles:
        if prior_close is None:
            ranges.append(candle.high - candle.low)
        else:
            ranges.append(
                max(
                    candle.high - candle.low,
                    abs(candle.high - prior_close),
                    abs(candle.low - prior_close),
                )
            )
        prior_close = candle.close
    return ranges


def atr(candles: list[CompletedCandle], period: int) -> Decimal | None:
    """Wilder ATR over completed candles.

    Deterministic: seeded with the simple mean of the first ``period`` gap-aware true
    ranges, then Wilder-smoothed. Returns ``None`` until enough closed bars exist.
    """
    if period < 1:
        return None
    gap_aware = true_ranges(candles)[1:]  # drop the seedless first bar
    if len(gap_aware) < period:
        return None
    value = sum(gap_aware[:period], start=Decimal("0")) / Decimal(period)
    for range_value in gap_aware[period:]:
        value = (value * Decimal(period - 1) + range_value) / Decimal(period)
    return value


def vwap_bands(candles: list[CompletedCandle]) -> dict[str, float | None]:
    """Session VWAP plus volume-weighted standard-deviation bands at 1σ and 2σ."""
    weighted: list[tuple[Decimal, int]] = []
    numerator = Decimal("0")
    denominator = 0
    for candle in candles:
        if candle.volume <= 0:
            continue
        typical_price = (candle.high + candle.low + candle.close) / Decimal("3")
        weighted.append((typical_price, candle.volume))
        numerator += typical_price * candle.volume
        denominator += candle.volume
    if denominator == 0:
        return {"vwap": None, "sigma": None, "upper_1": None, "lower_1": None, "upper_2": None, "lower_2": None}
    vwap = numerator / denominator
    variance = sum((weight * (price - vwap) ** 2 for price, weight in weighted), start=Decimal("0")) / denominator
    sigma = variance.sqrt() if variance > 0 else Decimal("0")
    return {
        "vwap": _decimal(vwap),
        "sigma": _decimal(sigma),
        "upper_1": _decimal(vwap + sigma),
        "lower_1": _decimal(vwap - sigma),
        "upper_2": _decimal(vwap + sigma * Decimal("2")),
        "lower_2": _decimal(vwap - sigma * Decimal("2")),
    }


def prior_day_levels(daily_candles: list[CompletedCandle]) -> dict[str, float | None]:
    """Close, high and low of the most recent completed daily candle."""
    ordered = sorted(daily_candles, key=lambda candle: candle.opened_at)
    if not ordered:
        return {"close": None, "high": None, "low": None}
    prior = ordered[-1]
    return {"close": _decimal(prior.close), "high": _decimal(prior.high), "low": _decimal(prior.low)}


def daily_atr(daily_candles: list[CompletedCandle], period: int) -> Decimal | None:
    """Wilder ATR over daily candles (same estimator as the intraday ``atr``)."""
    return atr(sorted(daily_candles, key=lambda candle: candle.opened_at), period)


def time_of_day_relative_volume(
    active: list[CompletedCandle],
    baseline: dict[str, float] | None,
) -> float | None:
    """Latest bar volume divided by the average volume at the same IST minute over prior sessions."""
    if not active or not baseline:
        return None
    minute_key = active[-1].opened_at.astimezone(MARKET_TIMEZONE).strftime("%H:%M")
    average = baseline.get(minute_key)
    if not average or average <= 0:
        return None
    return round(active[-1].volume / average, 4)


def opening_range(candles: list[CompletedCandle], minutes: int) -> dict[str, float | bool | None]:
    if not candles:
        return {"high": None, "low": None, "complete": False}
    session = candles[-1].session_date
    start = datetime.combine(session, MARKET_OPEN, tzinfo=MARKET_TIMEZONE)
    end = start + timedelta(minutes=minutes)
    range_candles = [candle for candle in candles if start <= candle.opened_at.astimezone(MARKET_TIMEZONE) < end]
    if not range_candles:
        return {
            "high": None,
            "low": None,
            "complete": candles[-1].opened_at.astimezone(MARKET_TIMEZONE) >= end,
        }
    return {
        "high": _decimal(max(candle.high for candle in range_candles)),
        "low": _decimal(min(candle.low for candle in range_candles)),
        "complete": candles[-1].closed_at.astimezone(MARKET_TIMEZONE) >= end,
    }


def relative_strength(stock: list[CompletedCandle], benchmark: list[CompletedCandle]) -> dict[str, float | None]:
    """Return stock return less NIFTY return, using only closed session candles."""
    benchmark_by_open = {candle.opened_at: candle for candle in benchmark}
    aligned = [
        (candle, benchmark_by_open[candle.opened_at]) for candle in stock if candle.opened_at in benchmark_by_open
    ]
    if len(aligned) < 2:
        return {
            "stock_return_percent": None,
            "nifty_return_percent": None,
            "relative_strength_percent": None,
        }
    first_stock, first_benchmark = aligned[0]
    latest_stock, latest_benchmark = aligned[-1]
    stock_return = ((latest_stock.close / first_stock.open) - Decimal("1")) * Decimal("100")
    benchmark_return = ((latest_benchmark.close / first_benchmark.open) - Decimal("1")) * Decimal("100")
    return {
        "stock_return_percent": _decimal(stock_return),
        "nifty_return_percent": _decimal(benchmark_return),
        "relative_strength_percent": _decimal(stock_return - benchmark_return),
    }


def nifty_regime(candles: list[CompletedCandle], fast_period: int, slow_period: int) -> dict[str, float | str | None]:
    if fast_period >= slow_period:
        raise ValueError("EMA fast period must be less than EMA slow period")
    if len(candles) < slow_period:
        return {"regime": "INSUFFICIENT_DATA", "ema_fast": None, "ema_slow": None, "vwap": None}
    fast = ema(candles, fast_period)
    slow = ema(candles, slow_period)
    vwap = session_vwap(candles)
    assert fast is not None and slow is not None
    close = candles[-1].close
    if vwap is not None and close >= vwap and fast > slow:
        regime = "BULLISH"
    elif vwap is not None and close <= vwap and fast < slow:
        regime = "BEARISH"
    else:
        regime = "NEUTRAL"
    return {
        "regime": regime,
        "ema_fast": _decimal(fast),
        "ema_slow": _decimal(slow),
        "vwap": _decimal(vwap) if vwap else None,
    }


def india_vix_state(
    level: float | None,
    prior_level: float | None,
    *,
    calm_below: float,
    stressed_above: float,
    extreme_above: float,
) -> dict[str, float | str | None] | None:
    """Classify the India VIX level into calm / normal / stressed / extreme."""
    if level is None or level <= 0:
        return None
    change_percent = round((level - prior_level) / prior_level * 100, 2) if prior_level and prior_level > 0 else 0.0
    if level >= extreme_above:
        state = "EXTREME"
    elif level >= stressed_above:
        state = "STRESSED"
    elif level <= calm_below:
        state = "CALM"
    else:
        state = "NORMAL"
    return {
        "level": round(level, 2),
        "prior_level": round(prior_level, 2) if prior_level else None,
        "change_percent": change_percent,
        "state": state,
    }


def market_breadth(above_vwap: int, total: int) -> dict[str, float | int | str] | None:
    """Fraction of tracked instruments trading above their session VWAP."""
    if total <= 0:
        return None
    ratio = round(above_vwap / total, 4)
    state = "EXPANSION" if ratio >= 0.6 else "CONTRACTION" if ratio <= 0.4 else "MIXED"
    return {"above_vwap": above_vwap, "total": total, "ratio": ratio, "state": state}


def _bounded(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def compose_market_regime(
    nifty_regime_name: str | None,
    nifty_vs_prior_close: float | None,
    vix_state: dict | None,
    breadth: dict | None,
) -> dict[str, object]:
    """Blend intraday NIFTY structure, India VIX and breadth into a tradable regime.

    Deterministic. ``score`` runs 0 (risk-off) to 100 (risk-on); the side gates and size
    multiplier are derived from the score and the VIX state.
    """
    score = 50.0
    components: dict[str, float] = {}

    if nifty_regime_name == "BULLISH":
        components["nifty_structure"] = 15.0
    elif nifty_regime_name == "BEARISH":
        components["nifty_structure"] = -15.0
    else:
        components["nifty_structure"] = 0.0

    components["nifty_vs_prior_close"] = (
        _bounded(nifty_vs_prior_close * 3.0, -12.0, 12.0) if nifty_vs_prior_close is not None else 0.0
    )
    components["breadth"] = _bounded((breadth["ratio"] - 0.5) * 40.0, -20.0, 20.0) if breadth else 0.0

    vix_name = vix_state["state"] if vix_state else "NORMAL"
    components["vix_level"] = {"CALM": 8.0, "NORMAL": 0.0, "STRESSED": -15.0, "EXTREME": -30.0}[vix_name]
    components["vix_change"] = -8.0 if vix_state and float(vix_state.get("change_percent", 0.0)) > 10.0 else 0.0

    score = _bounded(score + sum(components.values()), 0.0, 100.0)
    regime = "RISK_ON" if score >= 58 else "RISK_OFF" if score <= 42 else "NEUTRAL"

    if vix_name == "EXTREME":
        allow_long = allow_short = False
        size_multiplier = 0.0
        reason = "India VIX is extreme; stand aside"
    else:
        allow_long = score >= 40.0
        allow_short = score <= 60.0
        size_multiplier = 0.5 if vix_name == "STRESSED" else 1.0
        reason = f"{regime} (score {round(score)})"

    return {
        "regime": regime,
        "score": round(score, 2),
        "allow_long": allow_long,
        "allow_short": allow_short,
        "size_multiplier": size_multiplier,
        "components": {key: round(value, 2) for key, value in components.items()},
        "vix": vix_state,
        "breadth": breadth,
        "nifty_regime": nifty_regime_name,
        "reason": reason,
    }


def indicator_snapshot(
    candles: list[CompletedCandle],
    benchmark: list[CompletedCandle],
    *,
    opening_range_minutes: int,
    fast_ema_period: int,
    slow_ema_period: int,
    volume_lookback: int,
    is_nifty: bool,
    atr_period: int = 14,
    daily_candles: list[CompletedCandle] | None = None,
    time_of_day_volume_baseline: dict[str, float] | None = None,
) -> dict[str, object]:
    """Build a serializable snapshot for the latest completed candle only."""
    active = session_candles(candles)
    active_benchmark = session_candles(benchmark, active[-1].session_date) if active else []
    vwap = session_vwap(active)
    fast = ema(active, fast_ema_period)
    slow = ema(active, slow_ema_period)
    atr_value = atr(active, atr_period)
    last_close = active[-1].close if active else None
    bands = vwap_bands(active)
    opening = opening_range(active, opening_range_minutes)

    daily = daily_candles or []
    prior = prior_day_levels(daily)
    prior_close = Decimal(str(prior["close"])) if prior["close"] is not None else None
    session_open = active[0].open if active else None
    daily_atr_value = daily_atr(daily, atr_period)

    def _distance_atr(level: object) -> float | None:
        if level is None or not atr_value or atr_value <= 0 or last_close is None:
            return None
        return round(float((Decimal(str(level)) - last_close) / atr_value), 4)

    values: dict[str, object] = {
        "computed_at": datetime.now(UTC).isoformat(),
        "last_close": _decimal(last_close) if last_close is not None else None,
        "vwap": _decimal(vwap) if vwap else None,
        "ema_fast": _decimal(fast) if fast else None,
        "ema_slow": _decimal(slow) if slow else None,
        "opening_range": opening,
        "volume": volume_metrics(active, volume_lookback),
        "atr": _decimal(atr_value) if atr_value else None,
        "atr_percent": (
            _decimal(atr_value / last_close * Decimal("100")) if atr_value and last_close and last_close > 0 else None
        ),
        "vwap_bands": bands,
        "opening_range_atr": (
            round(float((Decimal(str(opening["high"])) - Decimal(str(opening["low"]))) / atr_value), 4)
            if atr_value and atr_value > 0 and opening.get("high") is not None and opening.get("low") is not None
            else None
        ),
        "extension_atr": (
            round(float(abs(last_close - vwap) / atr_value), 4)
            if atr_value and atr_value > 0 and vwap is not None and last_close is not None
            else None
        ),
        "prior_day": prior,
        "gap_percent": (
            _decimal((session_open - prior_close) / prior_close * Decimal("100"))
            if session_open is not None and prior_close and prior_close > 0
            else None
        ),
        "daily_atr": _decimal(daily_atr_value) if daily_atr_value else None,
        "daily_atr_percent": (
            _decimal(daily_atr_value / last_close * Decimal("100"))
            if daily_atr_value and last_close and last_close > 0
            else None
        ),
        "distance_to_prior_high_atr": _distance_atr(prior["high"]),
        "distance_to_prior_low_atr": _distance_atr(prior["low"]),
        "time_of_day_rvol": time_of_day_relative_volume(active, time_of_day_volume_baseline),
    }
    values["relative_strength"] = (
        {
            "stock_return_percent": 0.0,
            "nifty_return_percent": 0.0,
            "relative_strength_percent": 0.0,
        }
        if is_nifty
        else relative_strength(active, active_benchmark)
    )
    values["nifty_regime"] = nifty_regime(active, fast_ema_period, slow_ema_period) if is_nifty else None
    return values
