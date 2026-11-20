"""Swing setups on daily candles: what to buy tomorrow, and where it is wrong.

Phase 2 of the swing desk. Pure functions: a list of adjusted daily candles in,
a setup or nothing out. No database, no broker, no clock -- the same function
answers the backtest and, later, the evening scan, so the two can never
disagree about what a setup is.

**Long only.** The cash segment cannot hold a short overnight.

**No lookahead.** Every function reads ``bars[: i + 1]`` -- the candles up to
and including the evening it is evaluated on -- and returns an order for the
*next* session: a buy-stop above that evening's high. Nothing about the next
day is known when the setup is made.

These are hypotheses to test, not claims. Neither strategy is said to be
profitable; the backtest's out-of-sample half is what says anything, and then
only after charges.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from app.services.market_calculations import CompletedCandle

TICK = Decimal("0.05")
TREND_PULLBACK = "swing-trend-pullback-v1"
BASE_BREAKOUT = "swing-base-breakout-v1"


# --- indicators -----------------------------------------------------------------
#
# Lists aligned with the bars: value[i] uses bars[0..i] only, and is None until
# there is enough history. Floats inside, Decimal at the edges, because these
# are comparisons and rankings, not money.


def sma(values: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    running = 0.0
    for i, value in enumerate(values):
        running += value
        if i >= period:
            running -= values[i - period]
        if i >= period - 1:
            out[i] = running / period
    return out


def ema(values: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    current = sum(values[:period]) / period
    out[period - 1] = current
    for i in range(period, len(values)):
        current = values[i] * k + current * (1 - k)
        out[i] = current
    return out


def atr(bars: Sequence[CompletedCandle], period: int = 14) -> list[float | None]:
    """Wilder's average true range."""
    out: list[float | None] = [None] * len(bars)
    ranges: list[float] = []
    for i, bar in enumerate(bars):
        high, low = float(bar.high), float(bar.low)
        if i == 0:
            ranges.append(high - low)
        else:
            prev = float(bars[i - 1].close)
            ranges.append(max(high - low, abs(high - prev), abs(low - prev)))
    if len(bars) < period:
        return out
    current = sum(ranges[:period]) / period
    out[period - 1] = current
    for i in range(period, len(bars)):
        current = (current * (period - 1) + ranges[i]) / period
        out[i] = current
    return out


@dataclass(frozen=True)
class Series:
    """Everything a strategy reads about one share, computed once per share."""

    bars: Sequence[CompletedCandle]
    close: list[float]
    sma50: list[float | None]
    sma200: list[float | None]
    ema20: list[float | None]
    atr14: list[float | None]
    avg_volume20: list[float | None]

    @classmethod
    def of(cls, bars: Sequence[CompletedCandle]) -> "Series":
        close = [float(bar.close) for bar in bars]
        return cls(
            bars=bars,
            close=close,
            sma50=sma(close, 50),
            sma200=sma(close, 200),
            ema20=ema(close, 20),
            atr14=atr(bars, 14),
            avg_volume20=sma([float(bar.volume) for bar in bars], 20),
        )


def relative_strength(series: Series, benchmark_close: dict, i: int, lookback: int = 63) -> float | None:
    """The share's return over ``lookback`` sessions minus NIFTY's, in percent.

    Matched on session dates rather than positions, so a share with a missing
    day is compared against the index over the same calendar span.
    """
    if i < lookback:
        return None
    today, then = series.bars[i].session_date, series.bars[i - lookback].session_date
    index_now, index_then = benchmark_close.get(today), benchmark_close.get(then)
    if not index_now or not index_then or not series.close[i - lookback]:
        return None
    share = series.close[i] / series.close[i - lookback] - 1
    index = index_now / index_then - 1
    return (share - index) * 100


# --- setups ---------------------------------------------------------------------


@dataclass(frozen=True)
class SwingSetup:
    strategy: str
    signal_date: object  # date of the evening the setup was found
    entry_trigger: Decimal  # buy-stop for the next session
    initial_stop: Decimal
    target: Decimal  # where half is taken (2R)
    rank: float  # higher is better when there are more setups than slots
    max_holding_days: int
    trail: str  # "ema20" or "low10"
    reasons: dict = field(default_factory=dict)

    @property
    def risk_per_share(self) -> Decimal:
        return self.entry_trigger - self.initial_stop


def _price(value: float) -> Decimal:
    """A price on the NSE tick, rounded to the nearest 0.05."""
    return (Decimal(str(value)) / TICK).quantize(Decimal("1")) * TICK


def _bounded_stop(trigger: float, structural: float, atr_now: float, *, widest_atr: float) -> float | None:
    """The structural stop, but never wider than ``widest_atr`` ATRs and never closer than half of one.

    Too close and ordinary noise takes it; too wide and the position is so small
    the trade is not worth its charges. Either way the setup is skipped rather
    than forced.
    """
    stop = max(structural, trigger - widest_atr * atr_now)
    if trigger - stop < 0.5 * atr_now:
        return None
    return stop


def trend_pullback(series: Series, benchmark_close: dict, i: int) -> SwingSetup | None:
    """A share in a rising trend, stronger than NIFTY, that has pulled back to its 20 EMA.

    * Trend: close above the 50-day average, the 50 above the 200.
    * Strength: 63-day return beats NIFTY's.
    * Pullback: a low within 1% of the 20 EMA in the last three sessions, and
      today's close back above it.
    * Entry: a buy-stop one tick above today's high.
    * Stop: the lowest low of five sessions, bounded to 0.5-1.5 ATR.
    * Exit: half at 2R, the rest on a close below the 20 EMA, or after 12 sessions.
    """
    if i < 200:
        return None
    close, sma50, sma200, ema20, atr_now = (
        series.close[i],
        series.sma50[i],
        series.sma200[i],
        series.ema20[i],
        series.atr14[i],
    )
    if None in (sma50, sma200, ema20, atr_now) or not atr_now:
        return None
    if not (close > sma50 > sma200):
        return None
    rs = relative_strength(series, benchmark_close, i)
    if rs is None or rs <= 0:
        return None
    touched = any(
        series.ema20[j] is not None and float(series.bars[j].low) <= series.ema20[j] * 1.01 for j in range(i - 2, i + 1)
    )
    if not touched or close <= ema20:
        return None
    trigger = float(series.bars[i].high) + float(TICK)
    swing_low = min(float(bar.low) for bar in series.bars[i - 4 : i + 1])
    stop = _bounded_stop(trigger, swing_low, atr_now, widest_atr=1.5)
    if stop is None:
        return None
    entry, stop_price = _price(trigger), _price(stop)
    risk = entry - stop_price
    if risk <= 0:
        return None
    return SwingSetup(
        strategy=TREND_PULLBACK,
        signal_date=series.bars[i].session_date,
        entry_trigger=entry,
        initial_stop=stop_price,
        target=entry + 2 * risk,
        rank=rs,
        max_holding_days=12,
        trail="ema20",
        reasons={"rs_vs_nifty_pct": round(rs, 2), "close": close, "ema20": round(ema20, 2), "atr14": round(atr_now, 2)},
    )


def base_breakout(series: Series, benchmark_close: dict, i: int) -> SwingSetup | None:
    """A tight 20-session base, broken on a close above it with volume.

    * Base: the 20 sessions before today span at most 12% of price.
    * Trend: close above the 50-day average.
    * Breakout: today's close above the base's high, on at least 1.5x the
      20-session average volume.
    * Entry: a buy-stop one tick above today's high.
    * Stop: the base's low, bounded to 0.5-2 ATR.
    * Exit: half at 2R, the rest on a close below the 10-session low, or after
      15 sessions.
    """
    if i < 60:
        return None
    close, sma50, atr_now, avg_volume = series.close[i], series.sma50[i], series.atr14[i], series.avg_volume20[i - 1]
    if None in (sma50, atr_now, avg_volume) or not atr_now or not avg_volume:
        return None
    base = series.bars[i - 20 : i]
    base_high = max(float(bar.high) for bar in base)
    base_low = min(float(bar.low) for bar in base)
    if close <= sma50 or (base_high - base_low) / close > 0.12:
        return None
    if close <= base_high or float(series.bars[i].volume) < 1.5 * avg_volume:
        return None
    trigger = float(series.bars[i].high) + float(TICK)
    stop = _bounded_stop(trigger, base_low, atr_now, widest_atr=2.0)
    if stop is None:
        return None
    entry, stop_price = _price(trigger), _price(stop)
    risk = entry - stop_price
    if risk <= 0:
        return None
    rs = relative_strength(series, benchmark_close, i)
    return SwingSetup(
        strategy=BASE_BREAKOUT,
        signal_date=series.bars[i].session_date,
        entry_trigger=entry,
        initial_stop=stop_price,
        target=entry + 2 * risk,
        rank=rs if rs is not None else 0.0,
        max_holding_days=15,
        trail="low10",
        reasons={
            "base_range_pct": round((base_high - base_low) / close * 100, 2),
            "volume_vs_avg": round(float(series.bars[i].volume) / avg_volume, 2),
            "atr14": round(atr_now, 2),
        },
    )


STRATEGIES = {TREND_PULLBACK: trend_pullback, BASE_BREAKOUT: base_breakout}


def trail_level(series: Series, i: int, trail: str) -> float | None:
    """Where the trailing exit sits after the close of session ``i``.

    Read on closes, not intraday: the remainder is sold the next morning after
    a *close* below the level, which is what a swing trader acting once a day
    can actually do.
    """
    if trail == "ema20":
        return series.ema20[i]
    if trail == "low10" and i >= 10:
        return min(float(bar.low) for bar in series.bars[i - 10 : i])
    return None
