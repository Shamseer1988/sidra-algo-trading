"""Swing desk Phase 2: indicators, setups, and the backtest's fill and exit rules.

The backtest's rules are tested with a stub strategy that fires on a chosen
evening, against hand-made candles, so each rule is checked by itself: the
fill at the trigger, the skipped gap, the gap through a stop, the same-day
stop-and-target, the partial at 2R, the trail, the time limit, the charges and
the desk's limits. The real strategies are tested for what they claim and for
not reading the future.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest

from app.services.market_calculations import CompletedCandle
from app.services.swing_backtest import CostModel, SwingRules, metrics, run_backtest, split
from app.services.swing_data import MARKET_TIMEZONE
from app.services.swing_strategies import (
    TREND_PULLBACK,
    Series,
    SwingSetup,
    atr,
    ema,
    relative_strength,
    sma,
    trend_pullback,
)

START = date(2025, 1, 1)
FREE = CostModel(
    brokerage_per_order=Decimal("0"),
    stt_rate=Decimal("0"),
    exchange_rate=Decimal("0"),
    sebi_rate=Decimal("0"),
    stamp_rate=Decimal("0"),
    gst_rate=Decimal("0"),
    dp_charge_per_sale=Decimal("0"),
)


def day(n: int) -> date:
    return START + timedelta(days=n)


def candle(token: str, n: int, o, h, l, c, volume: int = 1000) -> CompletedCandle:  # noqa: E741
    d = day(n)
    return CompletedCandle(
        instrument_token=token,
        timeframe_seconds=86_400,
        opened_at=datetime.combine(d, time(9, 15), tzinfo=MARKET_TIMEZONE),
        closed_at=datetime.combine(d, time(15, 30), tzinfo=MARKET_TIMEZONE),
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(l)),
        close=Decimal(str(c)),
        volume=volume,
        tick_count=1,
    )


def flat(token: str, days: int, price: float = 100) -> list[CompletedCandle]:
    return [candle(token, n, price, price + 0.5, price - 0.5, price) for n in range(days)]


def rising_index(days: int) -> list[CompletedCandle]:
    return [candle("NIFTY", n, 100 + n, 101 + n, 99 + n, 100 + n) for n in range(days)]


def stub(fire_on: int, trigger="101", stop="96", target="111", hold=12, trail="ema20", rank=1.0):
    """A strategy that fires once, on the evening of ``fire_on``."""

    def fn(series, _bench, i):
        if series.bars[i].session_date != day(fire_on):
            return None
        return SwingSetup(
            strategy="stub",
            signal_date=day(fire_on),
            entry_trigger=Decimal(trigger),
            initial_stop=Decimal(stop),
            target=Decimal(target),
            rank=rank,
            max_holding_days=hold,
            trail=trail,
        )

    return fn


RULES = SwingRules(regime_sma=2, strategies=("stub",))


def run(bars, *, strategy=None, rules=RULES, costs=FREE, sectors=None, extra=None):
    universe = {"A": bars, **(extra or {})}
    return run_backtest(
        universe,
        sectors or {},
        rising_index(len(bars)),
        rules=rules,
        costs=costs,
        strategies={"stub": strategy or stub(5)},
    )


# --- indicators ------------------------------------------------------------------


def test_sma_and_ema_wait_for_their_period_and_then_average():
    assert sma([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]
    values = ema([1, 2, 3, 4], 2)
    assert values[0] is None and values[1] == 1.5
    assert values[3] == pytest.approx(3.5, abs=0.2)


def test_atr_uses_the_gap_from_the_previous_close():
    bars = [candle("A", 0, 100, 101, 99, 100), candle("A", 1, 110, 111, 109, 110)]
    assert atr(bars, 2)[1] == pytest.approx((2 + 11) / 2)


def test_relative_strength_is_measured_over_the_same_dates_as_the_index():
    bars = [candle("A", n, 100 + 2 * n, 101 + 2 * n, 99 + 2 * n, 100 + 2 * n) for n in range(70)]
    bench = {day(n): 100 + n for n in range(70)}
    rs = relative_strength(Series.of(bars), bench, 69, lookback=63)
    assert rs is not None and rs > 0


# --- the real strategies ------------------------------------------------------------


def uptrend_with_pullback() -> list[CompletedCandle]:
    """250 rising sessions, a dip to the 20 EMA, then a close back above it."""
    bars = [candle("A", n, 100 + n * 0.5, 101 + n * 0.5, 99.5 + n * 0.5, 100.5 + n * 0.5) for n in range(250)]
    last = 100.5 + 249 * 0.5
    bars.append(candle("A", 250, last - 1, last - 0.5, last - 9, last - 6))
    bars.append(candle("A", 251, last - 6, last - 2, last - 7, last - 2.5))
    return bars


def test_trend_pullback_finds_a_strong_share_back_at_its_average():
    bars = uptrend_with_pullback()
    bench = {bar.session_date: 100.0 for bar in bars}  # a flat index: the share is stronger
    setup = trend_pullback(Series.of(bars), bench, len(bars) - 1)
    assert setup is not None
    assert setup.strategy == TREND_PULLBACK
    assert setup.entry_trigger > setup.initial_stop
    assert setup.target == setup.entry_trigger + 2 * setup.risk_per_share


def test_trend_pullback_ignores_a_falling_share():
    bars = [candle("A", n, 300 - n, 301 - n, 299 - n, 300 - n) for n in range(250)]
    bench = {bar.session_date: 100.0 for bar in bars}
    series = Series.of(bars)
    assert all(trend_pullback(series, bench, i) is None for i in range(len(bars)))


def test_a_setup_does_not_change_when_the_future_is_appended():
    """No lookahead: the evening's answer is the same whether or not later candles exist."""
    bars = uptrend_with_pullback()
    bench = {day(n): 100.0 for n in range(300)}
    i = len(bars) - 1
    before = trend_pullback(Series.of(bars), bench, i)
    future = bars + [candle("A", 252 + k, 50, 51, 40, 45) for k in range(30)]
    after = trend_pullback(Series.of(future), bench, i)
    assert before == after


# --- fills ----------------------------------------------------------------------


def test_the_entry_fills_at_the_trigger_the_next_session():
    bars = flat("A", 30)
    bars[6] = candle("A", 6, 100, 102, 99.5, 101.5)
    [trade] = run(bars).trades
    assert trade.entry_date == day(6)
    assert trade.entry_price == Decimal("101")


def test_a_session_that_opens_above_the_trigger_fills_at_the_open():
    bars = flat("A", 30)
    bars[6] = candle("A", 6, 101.5, 103, 101, 102)
    [trade] = run(bars).trades
    assert trade.entry_price == Decimal("101.5")


def test_a_gap_beyond_the_allowance_is_skipped_not_chased():
    bars = flat("A", 30)
    bars[6] = candle("A", 6, 104, 105, 103, 104)
    result = run(bars)
    assert result.trades == []
    assert result.skipped["gapped above trigger"] == 1


def test_a_plan_the_next_session_does_not_reach_lapses():
    result = run(flat("A", 30))
    assert result.trades == []
    assert result.skipped["not triggered"] == 1


# --- exits ----------------------------------------------------------------------


def entered(extra: dict[int, tuple]) -> list[CompletedCandle]:
    bars = flat("A", 40)
    bars[6] = candle("A", 6, 100, 102, 99.5, 101.5)
    for n, ohlc in extra.items():
        bars[n] = candle("A", n, *ohlc)
    return bars


def test_a_gap_through_the_stop_exits_at_the_open_and_costs_more_than_planned():
    [trade] = run(entered({8: (90, 91, 89, 90)})).trades
    assert trade.exit_reason == "stop"
    assert trade.proceeds == Decimal("90") * trade.quantity
    assert trade.r_multiple < Decimal("-1")


def test_stop_and_target_on_the_same_day_is_counted_as_the_stop():
    [trade] = run(entered({8: (101, 112, 95, 105)})).trades
    assert trade.exit_reason == "stop"
    assert not trade.partial_done


def test_half_is_taken_at_two_r_and_the_rest_stops_at_entry():
    [trade] = run(entered({8: (102, 111.5, 101.5, 110), 9: (109, 109.5, 100.5, 101)})).trades
    assert trade.partial_done
    assert trade.exit_reason == "breakeven stop"
    half = trade.quantity // 2
    assert trade.proceeds == Decimal("111") * half + Decimal("101") * (trade.quantity - half)


def test_the_rest_is_sold_at_the_next_open_after_a_close_below_the_trail():
    """Half at 2R, then a run at 110 lifts the 10-session low above entry; a close under it exits."""
    rising = {n: (110, 110.5, 109.5, 110) for n in range(9, 30)}
    rising[8] = (102, 111.5, 101.5, 110)
    rising[20] = (109, 109.5, 104, 105)  # closes under the 10-session low (109.5), above the 101 stop
    [trade] = run(entered(rising), strategy=stub(5, trail="low10", hold=60)).trades
    assert trade.partial_done
    assert trade.exit_reason == "trail"
    assert trade.exit_date == day(21)
    assert trade.proceeds - Decimal("111") * (trade.quantity // 2) == Decimal("110") * (
        trade.quantity - trade.quantity // 2
    )


def test_a_trade_that_goes_nowhere_is_closed_by_the_time_limit():
    [trade] = run(entered({}), strategy=stub(5, hold=3)).trades
    assert trade.exit_reason == "time"
    assert trade.holding_days == 3


# --- money -------------------------------------------------------------------------


def test_delivery_charges_on_a_lakh_bought():
    # STT 100 + stamp 15 + (brokerage 20 + exchange 2.97 + SEBI 0.10) x 1.18
    assert CostModel().buy(Decimal("100000")).quantize(Decimal("0.01")) == Decimal("142.22")


def test_a_sale_pays_the_depository_charge():
    assert CostModel().sell(Decimal("100000")) - CostModel(dp_charge_per_sale=Decimal("0")).sell(
        Decimal("100000")
    ) == Decimal("20")


def test_charges_come_off_the_result():
    bars = entered({8: (90, 91, 89, 90)})
    free, charged = run(bars).trades[0], run(bars, costs=CostModel()).trades[0]
    assert charged.net_pnl < free.net_pnl
    assert charged.charges > 0


def test_size_is_risk_divided_by_the_stop_distance_and_capped_by_value():
    [trade] = run(entered({})).trades
    # Rs 750 of risk over a 5-rupee stop is 150 shares; 35% of a lakh at 101 caps it at 346.
    assert trade.quantity == 150


# --- the desk's limits ---------------------------------------------------------------


def test_one_share_per_sector():
    bars_b = entered({})
    for i, bar in enumerate(bars_b):
        bars_b[i] = CompletedCandle(**{**bar.__dict__, "instrument_token": "B"})
    result = run(entered({}), sectors={"A": "Banks", "B": "Banks"}, extra={"B": bars_b})
    assert len(result.trades) == 1
    assert result.skipped["sector limit"] == 1


def test_open_risk_above_the_heat_cap_is_refused():
    rules = SwingRules(regime_sma=2, strategies=("stub",), heat_cap_percent=Decimal("0.5"))
    result = run(entered({}), rules=rules)
    assert result.trades == []
    assert result.skipped["heat cap"] == 1


def test_no_new_entries_while_nifty_is_below_its_average():
    falling = [candle("NIFTY", n, 200 - n, 201 - n, 199 - n, 200 - n) for n in range(40)]
    result = run_backtest({"A": entered({})}, {}, falling, rules=RULES, costs=FREE, strategies={"stub": stub(5)})
    assert result.trades == []


# --- reporting --------------------------------------------------------------------------


def test_metrics_and_the_out_of_sample_split():
    result = run(entered({8: (90, 91, 89, 90)}))
    summary = metrics(result.trades)
    assert summary.trades == 1 and summary.wins == 0
    assert summary.max_drawdown > 0
    cut, inside, outside = split(result, 0.7)
    assert cut == result.dates[int(len(result.dates) * 0.7)]
    assert inside == result.trades and outside == []
