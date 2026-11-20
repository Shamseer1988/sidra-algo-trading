"""Swing desk Phase 1: the universe, daily history, adjustment and coverage.

The pure parts are tested directly. The backfill is tested against a fake
Upstox server and the real test database, because the claims that matter are
about storage: that it never rewrites a day the intraday indicators already
hold, and that a re-run stores nothing new.
"""

import sys
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import delete, select

from app.db.models import CorporateEvent, MarketCandle
from app.db.session import SessionLocal
from app.services.market_calculations import CompletedCandle
from app.services.swing_data import (
    DAILY_TIMEFRAME_SECONDS,
    MARKET_TIMEZONE,
    adjust_for_events,
    backfill_daily_range,
    coverage,
    load_adjusted_daily,
    load_daily,
    parse_daily_row,
    suspected_splits,
    windows,
)
from app.services.swing_universe import SwingUniverseError, nifty200, parse_constituents

TOKEN = "NSE_EQ|INE0SWING001"


def bar(day: date, close: str, *, open_: str | None = None, token: str = TOKEN, volume: int = 1000) -> CompletedCandle:
    return CompletedCandle(
        instrument_token=token,
        timeframe_seconds=DAILY_TIMEFRAME_SECONDS,
        opened_at=datetime.combine(day, time(9, 15), tzinfo=MARKET_TIMEZONE),
        closed_at=datetime.combine(day, time(15, 30), tzinfo=MARKET_TIMEZONE),
        open=Decimal(open_ or close),
        high=Decimal(close) + 1,
        low=Decimal(close) - 1,
        close=Decimal(close),
        volume=volume,
        tick_count=1,
    )


# --- the universe ----------------------------------------------------------------


def test_the_committed_list_is_the_nifty_200_with_upstox_keys():
    members = nifty200()
    assert len(members) == 200
    assert len({member.instrument_token for member in members}) == 200
    assert all(member.instrument_token.startswith("NSE_EQ|INE") for member in members)
    assert all(member.industry for member in members)


HEADER = "Company Name,Industry,Symbol,Series,ISIN Code"


def test_a_short_list_is_refused_rather_than_shrinking_the_universe():
    with pytest.raises(SwingUniverseError, match="1 members"):
        parse_constituents([HEADER, "ABB India Ltd.,Capital Goods,ABB,EQ,INE117A01022"])


def test_a_duplicated_isin_is_refused():
    row = "ABB India Ltd.,Capital Goods,ABB,EQ,INE117A01022"
    with pytest.raises(SwingUniverseError, match="more than once"):
        parse_constituents([HEADER, row, row], expected=None)


def test_a_file_without_the_isin_column_is_refused():
    with pytest.raises(SwingUniverseError, match="missing columns"):
        parse_constituents(["Company Name,Industry,Symbol,Series", "ABB,Capital Goods,ABB,EQ"], expected=None)


# --- parsing and windows ----------------------------------------------------------


def test_a_daily_row_is_stamped_like_the_intraday_sides_daily_rows():
    candle = parse_daily_row(["2025-06-12T00:00:00+05:30", 100, 105, 99, 104, 12345, 0], TOKEN)
    assert candle.session_date == date(2025, 6, 12)
    assert candle.opened_at == datetime(2025, 6, 12, 9, 15, tzinfo=MARKET_TIMEZONE)
    assert candle.timeframe_seconds == 86_400
    assert parse_daily_row(["2025-06-12", 1], TOKEN) is None
    assert parse_daily_row(["not-a-date", 1, 2, 3, 4, 5], TOKEN) is None


def test_two_years_is_walked_a_year_at_a_time():
    spans = windows(date(2024, 1, 1), date(2025, 12, 31))
    assert spans[0][0] == date(2024, 1, 1)
    assert spans[-1][1] == date(2025, 12, 31)
    assert all((end - start).days < 365 for start, end in spans)
    # Contiguous: every day asked for exactly once, no gap and no overlap.
    assert all(later[0] == (earlier[1] + timedelta(days=1)) for earlier, later in zip(spans, spans[1:], strict=False))


# --- adjustment ------------------------------------------------------------------


def event(ex_date: date, factor: str):
    return SimpleNamespace(ex_date=ex_date, price_factor=Decimal(factor))


def test_prices_before_a_split_are_divided_and_volume_multiplied():
    bars = [bar(date(2025, 6, 11), "200", volume=100), bar(date(2025, 6, 12), "101", open_="100")]
    adjusted = adjust_for_events(bars, [event(date(2025, 6, 12), "2")])
    assert adjusted[0].close == Decimal("100")
    assert adjusted[0].volume == 200
    assert adjusted[1] is bars[1]


def test_two_events_compound_on_the_oldest_prices():
    bars = [bar(date(2024, 1, 2), "400"), bar(date(2025, 1, 2), "200"), bar(date(2026, 1, 2), "100")]
    adjusted = adjust_for_events(bars, [event(date(2024, 6, 1), "2"), event(date(2025, 6, 1), "2")])
    assert [b.close for b in adjusted] == [Decimal("100"), Decimal("100"), Decimal("100")]


def test_no_events_means_the_bars_are_untouched():
    bars = [bar(date(2025, 6, 11), "200")]
    assert adjust_for_events(bars, []) == bars


# --- split detection -------------------------------------------------------------


def test_an_overnight_halving_is_suspected_as_a_one_to_two_split():
    bars = [bar(date(2025, 6, 11), "2000"), bar(date(2025, 6, 12), "1010", open_="990")]
    [suspect] = suspected_splits(bars)
    assert suspect.ex_date == date(2025, 6, 12)
    assert suspect.nearest_factor == Decimal("2")


def test_a_big_fall_that_matches_no_ratio_is_not_called_a_split():
    bars = [bar(date(2025, 6, 11), "1000"), bar(date(2025, 6, 12), "590", open_="590")]
    assert suspected_splits(bars) == []


def test_an_ordinary_day_is_not_suspected():
    bars = [bar(date(2025, 6, 11), "1000"), bar(date(2025, 6, 12), "980", open_="975")]
    assert suspected_splits(bars) == []


# --- coverage --------------------------------------------------------------------


def test_coverage_counts_only_index_days_within_the_shares_own_life():
    reference = [date(2025, 6, d) for d in (9, 10, 11, 12, 13)]
    bars = [bar(date(2025, 6, 10), "1"), bar(date(2025, 6, 13), "1")]
    result = coverage(TOKEN, bars, reference)
    assert result.first == date(2025, 6, 10)
    assert result.missing == [date(2025, 6, 11), date(2025, 6, 12)]
    assert not result.complete


def test_a_share_with_no_candles_has_no_coverage():
    assert coverage(TOKEN, [], [date(2025, 6, 9)]).sessions == 0


# --- storage, against the test database -------------------------------------------


@pytest.fixture
async def clean():
    async def wipe():
        async with SessionLocal() as session:
            await session.execute(delete(MarketCandle).where(MarketCandle.instrument_token == TOKEN))
            await session.execute(delete(CorporateEvent).where(CorporateEvent.instrument_token == TOKEN))
            await session.commit()

    await wipe()
    yield
    await wipe()


def upstox(rows: list[list], *, status: int = 200, calls: list | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        if status != 200:
            return httpx.Response(status, json={"status": "error"})
        return httpx.Response(200, json={"status": "success", "data": {"candles": rows}})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


ROWS = [
    ["2025-06-12T00:00:00+05:30", 100, 105, 99, 104, 1000, 0],
    ["2025-06-11T00:00:00+05:30", 98, 101, 97, 100, 900, 0],
]


async def test_a_backfill_stores_each_day_once_and_a_rerun_stores_nothing(clean):
    client = upstox(ROWS)
    first = await backfill_daily_range(
        [TOKEN], date(2025, 6, 1), date(2025, 6, 30), access_token="x", client=client, pause_seconds=0
    )
    second = await backfill_daily_range(
        [TOKEN], date(2025, 6, 1), date(2025, 6, 30), access_token="x", client=client, pause_seconds=0
    )
    assert first.stored[TOKEN] == 2
    assert second.stored[TOKEN] == 0
    async with SessionLocal() as session:
        bars = await load_daily(session, TOKEN)
    assert [b.session_date for b in bars] == [date(2025, 6, 11), date(2025, 6, 12)]


async def test_a_day_the_intraday_side_already_stored_is_never_rewritten(clean):
    """The intraday indicators keep their own daily rows; the backfill must not change them."""
    async with SessionLocal() as session:
        session.add(
            MarketCandle(
                instrument_token=TOKEN,
                timeframe_seconds=DAILY_TIMEFRAME_SECONDS,
                session_date=date(2025, 6, 12),
                opened_at=datetime.combine(date(2025, 6, 12), time(9, 15), tzinfo=MARKET_TIMEZONE),
                closed_at=datetime.combine(date(2025, 6, 12), time(15, 30), tzinfo=MARKET_TIMEZONE),
                open=Decimal("1"),
                high=Decimal("1"),
                low=Decimal("1"),
                close=Decimal("777"),
                volume=1,
                tick_count=1,
            )
        )
        await session.commit()
    report = await backfill_daily_range(
        [TOKEN], date(2025, 6, 1), date(2025, 6, 30), access_token="x", client=upstox(ROWS), pause_seconds=0
    )
    assert report.stored[TOKEN] == 1
    async with SessionLocal() as session:
        kept = await session.scalar(
            select(MarketCandle.close).where(
                MarketCandle.instrument_token == TOKEN, MarketCandle.session_date == date(2025, 6, 12)
            )
        )
    assert kept == Decimal("777")


async def test_a_refused_window_is_reported_and_stores_nothing(clean):
    report = await backfill_daily_range(
        [TOKEN], date(2025, 6, 1), date(2025, 6, 30), access_token="x", client=upstox([], status=400), pause_seconds=0
    )
    assert report.stored[TOKEN] == 0
    assert "HTTP 400" in report.failures[TOKEN][0]


async def test_only_a_confirmed_split_adjusts_what_a_strategy_reads(clean):
    rows = [
        ["2025-06-11T00:00:00+05:30", 200, 202, 198, 200, 100, 0],
        ["2025-06-12T00:00:00+05:30", 100, 102, 99, 101, 200, 0],
    ]
    await backfill_daily_range(
        [TOKEN], date(2025, 6, 1), date(2025, 6, 30), access_token="x", client=upstox(rows), pause_seconds=0
    )
    async with SessionLocal() as session:
        session.add(
            CorporateEvent(
                instrument_token=TOKEN,
                event_type="SPLIT",
                ex_date=date(2025, 6, 12),
                price_factor=Decimal("2"),
                confirmed=False,
            )
        )
        await session.commit()
        unconfirmed = await load_adjusted_daily(session, TOKEN)
        assert unconfirmed[0].close == Decimal("200")
        row = await session.scalar(select(CorporateEvent).where(CorporateEvent.instrument_token == TOKEN))
        row.confirmed = True
        await session.commit()
        confirmed = await load_adjusted_daily(session, TOKEN)
    assert confirmed[0].close == Decimal("100")
    assert confirmed[1].close == Decimal("101")


# --- the scripts ---------------------------------------------------------------------


def _script(name: str):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    try:
        return __import__(name)
    finally:
        sys.path.pop(0)


@pytest.mark.parametrize(
    ("moment", "refused"),
    [
        (datetime(2026, 11, 23, 10, 0, tzinfo=MARKET_TIMEZONE), True),  # Monday, in session
        (datetime(2026, 11, 23, 15, 30, tzinfo=MARKET_TIMEZONE), True),  # Monday, at the close
        (datetime(2026, 11, 23, 15, 40, tzinfo=MARKET_TIMEZONE), False),  # Monday, after the close
        (datetime(2026, 11, 21, 11, 0, tzinfo=MARKET_TIMEZONE), False),  # Saturday
    ],
)
def test_the_backfill_refuses_to_run_during_market_hours(moment, refused):
    assert _script("swing_backfill").market_hours_now(moment) is refused


@pytest.mark.parametrize(
    ("event_type", "factor", "amount", "ok"),
    [
        ("SPLIT", Decimal("2"), None, True),
        ("SPLIT", None, None, False),
        ("BONUS", Decimal("1"), None, False),
        ("RESULTS", None, None, True),
        ("RESULTS", Decimal("2"), None, False),
        ("DIVIDEND", None, Decimal("4"), True),
        ("DIVIDEND", None, None, False),
    ],
)
def test_a_corporate_event_that_would_adjust_wrongly_is_refused(event_type, factor, amount, ok):
    assert (_script("corporate_events").validated(event_type, factor, amount) is None) is ok
