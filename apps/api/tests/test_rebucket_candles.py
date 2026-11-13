"""Building a coarser candle out of stored minutes, without inventing one.

A three-minute candle is three one-minute candles, but only if the grouping,
the open, the close and the volume are each taken from the right place. Get the
open from the wrong minute and every replay at the new timeframe reads a
different market from the one that traded, with nothing to show it.
"""

import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import rebucket_candles as rebucket  # noqa: E402

from app.db.models import MarketCandle  # noqa: E402
from app.services.candle_aggregation import _bucket_start, fold_to_timeframe  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
SESSION = date(2026, 10, 16)
TOKEN = "NSE_EQ|TEST"


def minute(hhmm: str, *, open_: str, high: str, low: str, close: str, volume: int, ticks: int = 1) -> MarketCandle:
    hour, mins = (int(part) for part in hhmm.split(":"))
    opened = datetime(SESSION.year, SESSION.month, SESSION.day, hour, mins, tzinfo=IST)
    return MarketCandle(
        instrument_token=TOKEN,
        timeframe_seconds=60,
        session_date=SESSION,
        opened_at=opened,
        closed_at=opened + timedelta(seconds=60),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=volume,
        tick_count=ticks,
    )


def fold(rows, timeframe_seconds):
    return fold_to_timeframe([rebucket.as_completed(row) for row in rows], timeframe_seconds)


def key(candle):
    return (candle.instrument_token, candle.opened_at)


THREE = [
    minute("09:15", open_="100", high="104", low="99", close="103", volume=10, ticks=5),
    minute("09:16", open_="103", high="107", low="102", close="105", volume=20, ticks=7),
    minute("09:17", open_="105", high="106", low="97", close="98", volume=30, ticks=9),
]


# --- the grouping ---------------------------------------------------------


def test_the_session_open_is_a_three_minute_boundary():
    """09:15 IST has to start a bucket, or the first candle of every session is
    a partial one glued to the tail of a bucket that never traded."""
    opened = datetime(2026, 10, 16, 9, 15, tzinfo=IST)
    assert _bucket_start(opened, 180) == opened


def test_a_session_divides_into_whole_three_minute_candles():
    """375 minutes, 09:15 to 15:30. A remainder would mean one short candle a
    day at a predictable time, which is the kind of bias that looks like edge."""
    assert 375 % 3 == 0


def test_three_minutes_become_one_candle():
    built = fold(THREE, 180)
    assert len(built) == 1
    assert rebucket.minutes_per_bucket(THREE, 180)[key(built[0])] == 3


def test_minutes_in_different_buckets_do_not_merge():
    later = minute("09:18", open_="98", high="99", low="97", close="98", volume=5)
    built = fold([*THREE, later], 180)
    # Bucket starts come back in UTC, the way the live aggregator stores them.
    assert [bucket.opened_at.astimezone(IST).strftime("%H:%M") for bucket in built] == ["09:15", "09:18"]


def test_two_instruments_are_kept_apart():
    other = minute("09:15", open_="50", high="51", low="49", close="50", volume=1)
    other.instrument_token = "NSE_EQ|OTHER"
    built = fold([THREE[0], other], 180)
    assert {bucket.instrument_token for bucket in built} == {TOKEN, "NSE_EQ|OTHER"}


# --- the candle itself ----------------------------------------------------


def test_the_open_is_the_first_minute_and_the_close_the_last():
    candle = fold(THREE, 180)[0]
    assert candle.open == Decimal("100")
    assert candle.close == Decimal("98")


def test_the_open_follows_time_not_the_order_rows_arrived():
    """Rows come back ordered by the query, and a later query that does not
    order would otherwise decide the candle's open and close."""
    candle = fold(list(reversed(THREE)), 180)[0]
    assert candle.open == Decimal("100")
    assert candle.close == Decimal("98")


def test_the_high_and_low_span_every_minute():
    candle = fold(THREE, 180)[0]
    assert candle.high == Decimal("107")
    assert candle.low == Decimal("97")


def test_the_volume_is_added_up():
    """Stored volumes are already per candle: _volume_delta converts the feed's
    running total before a row is written, so these add."""
    candle = fold(THREE, 180)[0]
    assert candle.volume == 60
    assert candle.tick_count == 21


def test_the_candle_carries_the_timeframe_it_was_built_for():
    candle = fold(THREE, 180)[0]
    assert candle.timeframe_seconds == 180
    assert candle.session_date == SESSION
    assert candle.instrument_token == TOKEN


def test_closed_at_is_the_nominal_end_not_the_last_minute_present():
    """A bucket missing its final minute is still a candle of this timeframe.
    A closed_at that shrank with the gap would describe a timeframe that is not
    the one being replayed."""
    candle = fold(THREE[:2], 180)[0]
    assert candle.closed_at - candle.opened_at == timedelta(seconds=180)


def test_a_feed_gap_leaves_a_short_candle_rather_than_no_candle():
    """Dropping it would quietly shorten that session in every replay."""
    built = fold([THREE[0], THREE[2]], 180)
    assert len(built) == 1
    assert rebucket.minutes_per_bucket([THREE[0], THREE[2]], 180)[key(built[0])] == 2
    candle = built[0]
    assert candle.open == Decimal("100")
    assert candle.close == Decimal("98")


# --- five minutes, to show nothing is hard-coded to three -----------------


def test_a_five_minute_candle_groups_five_minutes():
    rows = [minute(f"09:{15 + index}", open_="100", high="101", low="99", close="100", volume=1) for index in range(5)]
    built = fold(rows, 300)
    assert len(built) == 1
    assert rebucket.minutes_per_bucket(rows, 300)[key(built[0])] == 5
    assert built[0].closed_at - built[0].opened_at == timedelta(seconds=300)


@pytest.mark.parametrize("bad", [60, 30, 90, 0])
def test_a_timeframe_that_is_not_whole_minutes_above_sixty_is_refused(bad: int):
    """Writing 90s rows would store a timeframe the live aggregator can be set
    to but no source candle divides into, so each row would be part invention."""
    assert bad <= rebucket.SOURCE_TIMEFRAME_SECONDS or bad % rebucket.SOURCE_TIMEFRAME_SECONDS
