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


def indicator_snapshot(
    candles: list[CompletedCandle],
    benchmark: list[CompletedCandle],
    *,
    opening_range_minutes: int,
    fast_ema_period: int,
    slow_ema_period: int,
    volume_lookback: int,
    is_nifty: bool,
) -> dict[str, object]:
    """Build a serializable snapshot for the latest completed candle only."""
    active = session_candles(candles)
    active_benchmark = session_candles(benchmark, active[-1].session_date) if active else []
    vwap = session_vwap(active)
    fast = ema(active, fast_ema_period)
    slow = ema(active, slow_ema_period)
    values: dict[str, object] = {
        "computed_at": datetime.now(UTC).isoformat(),
        "vwap": _decimal(vwap) if vwap else None,
        "ema_fast": _decimal(fast) if fast else None,
        "ema_slow": _decimal(slow) if slow else None,
        "opening_range": opening_range(active, opening_range_minutes),
        "volume": volume_metrics(active, volume_lookback),
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
