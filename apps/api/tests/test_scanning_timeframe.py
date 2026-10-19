"""The scanning timeframe is a setting, so nothing may assume it is 60.

``candle_timeframe_seconds`` is offered in the UI and bounded 60..900, but two
queries matched the literal 60 instead of reading it. Neither would raise on
any other setting; both would simply find no rows. That is the dangerous shape:

* ``universe._session_open`` feeds the gap component, a quarter of the
  composite score. No row means no gap, and a universe still gets chosen.
* ``MarketCalculationPersistenceService._time_of_day_baseline`` feeds RVOL. No
  rows means every volume is compared against a baseline that is not there.

These tests pin the timeframe each query asks for, without a database, by
reading it off the compiled statement. A test that needed rows would pass on a
fixture built at whatever timeframe the fixture chose.
"""

from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.candle_aggregation import MarketCalculationPersistenceService
from app.services.universe import _session_open


class CapturingSession:
    """Answers a query with nothing, and keeps what was asked."""

    def __init__(self) -> None:
        self.timeframes: list[int] = []

    def _capture(self, statement) -> None:
        compiled = statement.compile(compile_kwargs={"literal_binds": True})
        text = str(compiled)
        marker = "market_candles.timeframe_seconds = "
        for part in text.split(marker)[1:]:
            self.timeframes.append(int(part.split()[0].rstrip(")")))

    async def scalar(self, statement):
        self._capture(statement)
        return None

    async def scalars(self, statement):
        self._capture(statement)
        return SimpleNamespace(all=lambda: [])


@pytest.mark.parametrize("timeframe", [60, 180, 300, 900])
async def test_the_ranking_reads_the_session_open_at_the_scanning_timeframe(timeframe: int):
    session = CapturingSession()
    await _session_open(session, "NSE_EQ|TEST", date(2026, 10, 16), timeframe)
    assert session.timeframes == [timeframe]


@pytest.mark.parametrize("timeframe", [60, 180, 300, 900])
async def test_the_rvol_baseline_reads_the_scanning_timeframe(timeframe: int):
    service = MarketCalculationPersistenceService.__new__(MarketCalculationPersistenceService)
    service._settings = SimpleNamespace(candle_timeframe_seconds=timeframe, rvol_baseline_sessions=10)
    service._tod_baseline = {}
    session = CapturingSession()
    await service._time_of_day_baseline(session, "NSE_EQ|TEST", date(2026, 10, 16))
    assert session.timeframes == [timeframe]


def test_a_session_of_minutes_folds_to_whole_candles_not_relabelled_minutes():
    """What the day's backfill used to do, and must not.

    Both Upstox intraday endpoints are fixed at 1minute, so every candle that
    arrives is a minute. Stamping each one with candle_timeframe_seconds --
    which is what happened before -- stored 375 one-minute candles as 375
    three-minute candles: triple the rows, each claiming three minutes it did
    not cover, overlapping, and afterwards indistinguishable from candles the
    live feed built. Folded, the same session is 125 candles.
    """
    from datetime import timedelta, timezone

    from app.services.candle_aggregation import fold_to_timeframe
    from app.services.market_calculations import CompletedCandle

    ist = timezone(timedelta(hours=5, minutes=30))
    open_at = datetime(2026, 10, 16, 9, 15, tzinfo=ist)
    minutes = [
        CompletedCandle(
            instrument_token="NSE_EQ|TEST",
            timeframe_seconds=60,
            opened_at=open_at + timedelta(minutes=index),
            closed_at=open_at + timedelta(minutes=index + 1),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=1,
            tick_count=1,
        )
        for index in range(375)
    ]
    folded = fold_to_timeframe(minutes, 180)
    assert len(minutes) == 375
    assert len(folded) == 125
    assert {candle.timeframe_seconds for candle in folded} == {180}
    assert sum(candle.volume for candle in folded) == 375


def test_the_range_backfill_stores_the_timeframe_it_actually_fetched():
    """Research history is minutes because the endpoint only serves minutes.
    Coarser history is built from it by scripts/rebucket_candles.py, which is
    why this must not follow the setting."""
    from app.services.upstox_backfill import INTRADAY_SOURCE_TIMEFRAME_SECONDS, UPSTOX_RANGE_URL

    assert INTRADAY_SOURCE_TIMEFRAME_SECONDS == 60
    assert "1minute" in UPSTOX_RANGE_URL


def test_the_setting_allows_three_minute_candles():
    """A bound that stopped short of 180 would make the switch unreachable."""
    from app.services.indicator_settings import IndicatorSettings

    assert IndicatorSettings(candle_timeframe_seconds=180).candle_timeframe_seconds == 180
    with pytest.raises(ValueError):
        IndicatorSettings(candle_timeframe_seconds=59)
