"""The numbers that say whether a backtest's average means anything.

Pure functions, standard library only. Every input is a list of per-trade R
multiples (net of charges) unless stated.

* ``summary`` -- count, mean, standard deviation, t-statistic, win rate.
* ``bootstrap_interval`` -- a seeded 95% interval for the mean R
  (``backtest_sweep.bootstrap_interval``, reused).
* ``deflated_sharpe`` -- Bailey and López de Prado's deflated Sharpe ratio:
  the probability that the true per-trade Sharpe ratio is above zero *after*
  accounting for having chosen the best of ``trials`` variants, and for the
  returns' skew and fat tails. The best of forty random strategies has a
  positive Sharpe ratio by construction; this is how far above that
  expectation the chosen one has to be before it counts.
* ``cost_sensitivity`` -- mean R had charges been 1x, 1.5x and 2x.
* ``regime_split`` -- the same summary on days the benchmark rose and days it
  fell, so an edge that exists only in one market is visible as such.

References: D. H. Bailey and M. López de Prado, "The Deflated Sharpe Ratio:
Correcting for Selection Bias, Backtest Overfitting and Non-Normality",
Journal of Portfolio Management 40(5), 2014.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from decimal import Decimal
from statistics import NormalDist

from app.services.backtest_sweep import bootstrap_interval

EULER_GAMMA = 0.5772156649015329
_NORMAL = NormalDist()


def _floats(values: Iterable) -> list[float]:
    return [float(value) for value in values]


def summary(r_values: Iterable) -> dict:
    values = _floats(r_values)
    n = len(values)
    if n == 0:
        return {"trades": 0, "mean_r": None, "stdev_r": None, "t_stat": None, "win_rate": None}
    mean = sum(values) / n
    stdev = math.sqrt(sum((x - mean) ** 2 for x in values) / (n - 1)) if n > 1 else 0.0
    t_stat = mean / (stdev / math.sqrt(n)) if n > 1 and stdev > 0 else None
    return {
        "trades": n,
        "mean_r": round(mean, 4),
        "stdev_r": round(stdev, 4),
        "t_stat": round(t_stat, 3) if t_stat is not None else None,
        "win_rate": round(sum(1 for x in values if x > 0) / n, 4),
        "ci95_mean_r": bootstrap_interval(values),
    }


def _moments(values: list[float]) -> tuple[float, float, float, float]:
    n = len(values)
    mean = sum(values) / n
    variance = sum((x - mean) ** 2 for x in values) / n
    stdev = math.sqrt(variance)
    if stdev == 0:
        return mean, 0.0, 0.0, 3.0
    skew = sum(((x - mean) / stdev) ** 3 for x in values) / n
    kurtosis = sum(((x - mean) / stdev) ** 4 for x in values) / n
    return mean, stdev, skew, kurtosis


def expected_max_sharpe(trials: int, sharpe_variance: float) -> float:
    """The Sharpe ratio the best of ``trials`` skill-less variants is expected to show."""
    if trials <= 1 or sharpe_variance <= 0:
        return 0.0
    first = _NORMAL.inv_cdf(1 - 1 / trials)
    second = _NORMAL.inv_cdf(1 - 1 / (trials * math.e))
    return math.sqrt(sharpe_variance) * ((1 - EULER_GAMMA) * first + EULER_GAMMA * second)


def deflated_sharpe(r_values: Iterable, *, trials: int, sharpe_variance: float | None = None) -> dict:
    """Probability that the true per-trade Sharpe ratio exceeds what the best of ``trials`` would show by luck.

    ``sharpe_variance`` is the variance of the Sharpe ratios across the
    variants tried. Without it, the variance a single Sharpe estimate of this
    length would have under the null (1 / (n - 1)) is used, which is the
    conservative textbook default when the other variants' results are not
    to hand.

    Returns the observed Sharpe ratio, the threshold the selection bias
    implies, and the deflated probability (DSR). A DSR below 0.95 means the
    result cannot be told apart from the luck of choosing the best of many.
    """
    values = _floats(r_values)
    n = len(values)
    if n < 3:
        return {"sharpe": None, "threshold": None, "dsr": None, "trials": trials, "trades": n}
    mean, stdev, skew, kurtosis = _moments(values)
    if stdev == 0:
        return {"sharpe": None, "threshold": None, "dsr": None, "trials": trials, "trades": n}
    sharpe = mean / stdev
    variance = sharpe_variance if sharpe_variance is not None and sharpe_variance > 0 else 1.0 / (n - 1)
    threshold = expected_max_sharpe(max(trials, 1), variance)
    denominator = 1 - skew * sharpe + (kurtosis - 1) / 4 * sharpe**2
    if denominator <= 0:
        return {
            "sharpe": round(sharpe, 4),
            "threshold": round(threshold, 4),
            "dsr": None,
            "trials": trials,
            "trades": n,
        }
    z = (sharpe - threshold) * math.sqrt(n - 1) / math.sqrt(denominator)
    return {
        "sharpe": round(sharpe, 4),
        "threshold": round(threshold, 4),
        "dsr": round(_NORMAL.cdf(z), 4),
        "trials": trials,
        "trades": n,
        "skew": round(skew, 3),
        "kurtosis": round(kurtosis, 3),
    }


def sharpe_variance(variant_r_lists: Iterable[Iterable]) -> float | None:
    """Variance of the per-trade Sharpe ratios across the variants that were tried."""
    sharpes = []
    for values in variant_r_lists:
        floats = _floats(values)
        if len(floats) < 3:
            continue
        mean, stdev, _, _ = _moments(floats)
        if stdev > 0:
            sharpes.append(mean / stdev)
    if len(sharpes) < 2:
        return None
    centre = sum(sharpes) / len(sharpes)
    return sum((s - centre) ** 2 for s in sharpes) / (len(sharpes) - 1)


def cost_sensitivity(trades: Iterable) -> dict:
    """Mean R at 1x, 1.5x and 2x charges, from gross P&L, fees and planned risk."""
    rows = [trade for trade in trades if getattr(trade, "risk_amount", None) and trade.risk_amount > 0]
    if not rows:
        return {"x1": None, "x1_5": None, "x2": None}

    def mean_at(multiplier: Decimal) -> float:
        total = sum(((t.gross_pnl - t.fees_total * multiplier) / t.risk_amount for t in rows), start=Decimal("0"))
        return round(float(total / len(rows)), 4)

    return {"x1": mean_at(Decimal("1")), "x1_5": mean_at(Decimal("1.5")), "x2": mean_at(Decimal("2"))}


def benchmark_day_direction(benchmark_candles: Iterable) -> dict:
    """Session date -> "UP" or "DOWN" by the benchmark's close against its open that day."""
    first: dict = {}
    last: dict = {}
    for candle in benchmark_candles:
        day = candle.session_date
        if day not in first or candle.opened_at < first[day].opened_at:
            first[day] = candle
        if day not in last or candle.opened_at > last[day].opened_at:
            last[day] = candle
    return {day: "UP" if last[day].close >= first[day].open else "DOWN" for day in first}


def regime_split(trades: Iterable, directions: dict) -> dict:
    """The per-trade summary on benchmark-up days and benchmark-down days."""
    from datetime import date

    buckets: dict[str, list] = {"UP": [], "DOWN": [], "UNKNOWN": []}
    for trade in trades:
        day = date.fromisoformat(str(trade.session_date))
        buckets[directions.get(day, "UNKNOWN")].append(trade.realized_r)
    return {name: summary(values) for name, values in buckets.items() if values}
