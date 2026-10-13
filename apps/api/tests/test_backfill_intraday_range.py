"""Loading past sessions for research, without disturbing the live session.

Four sessions of 1-minute history is why a backtest over this watchlist
returned three trades, and why every settings question this week -- score
threshold, reward:risk, holding limit, which shares -- had no answer. This is
the load that gives those questions a distribution to look at.

The property that matters most here is a negative one. The live persistence
path recomputes indicators, rewrites data-quality history and can publish a
snapshot the scanner reads; run over hundreds of thousands of historical
candles it would overwrite live state mid-session. So the test below asserts
that a range backfill writes raw candles and nothing else.
"""

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select

from app.core.config import get_settings
from app.db.models import MarketCandle, MarketIndicatorSnapshot
from app.db.session import SessionLocal
from app.services import upstox_backfill
from app.services.upstox_backfill import (
    RangeBackfillReport,
    _parse_candle,
    _store_batch,
    _windows,
    backfill_intraday_range,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

pytestmark = pytest.mark.anyio

TOKEN = "NSE_EQ|BACKFILL-TEST"
OTHER = "NSE_EQ|BACKFILL-OTHER"
DAY = date(2026, 3, 4)  # a Wednesday the NSE calendar confirms and is not a holiday


def row(minute: int, *, day: date = DAY, price: str = "100") -> list:
    """One Upstox candle row: [timestamp, open, high, low, close, volume, oi].

    Timestamped in IST from 09:15, which is how Upstox sends them, so that a
    negative ``minute`` really is a pre-open print.
    """
    opened = datetime(day.year, day.month, day.day, 9, 15, tzinfo=upstox_backfill.MARKET_TIMEZONE) + timedelta(
        minutes=minute
    )
    return [opened.isoformat(), price, "101", "99", price, 1_000 + minute, 0]


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class FakeClient:
    """Stands in for httpx.AsyncClient. Records every URL it was asked for."""

    def __init__(self, answers, **_: object) -> None:
        self._answers = answers
        self.urls: list[str] = []

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def get(self, url: str) -> FakeResponse:
        self.urls.append(url)
        answer = self._answers(url) if callable(self._answers) else self._answers
        if isinstance(answer, Exception):
            raise answer
        return answer


async def clean() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(MarketCandle).where(MarketCandle.instrument_token.in_([TOKEN, OTHER])))
        await session.execute(
            delete(MarketIndicatorSnapshot).where(MarketIndicatorSnapshot.instrument_token.in_([TOKEN, OTHER]))
        )
        await session.commit()


@pytest.fixture(autouse=True)
async def reset():
    await clean()
    yield
    await clean()


def install(monkeypatch: pytest.MonkeyPatch, answers) -> FakeClient:
    client = FakeClient(answers)
    monkeypatch.setattr(upstox_backfill.httpx, "AsyncClient", lambda **kwargs: client)
    # Nothing in these tests is waiting on a rate limit.
    monkeypatch.setattr(upstox_backfill.asyncio, "sleep", _no_wait)
    return client


async def _no_wait(_seconds: float) -> None:
    return None


# --- the range, in request-sized pieces -----------------------------------


def test_a_range_is_walked_in_windows_oldest_first() -> None:
    """Upstox serves 1-minute candles a window at a time rather than for any
    span asked for, so six months is many requests, not one."""
    spans = _windows(date(2026, 1, 1), date(2026, 3, 1), size_days=25)
    assert spans[0] == (date(2026, 1, 1), date(2026, 1, 25))
    assert spans[1] == (date(2026, 1, 26), date(2026, 2, 19))
    assert spans[-1][1] == date(2026, 3, 1)
    # No gap and no overlap between consecutive windows: either would lose or
    # double-fetch a session.
    for earlier, later in zip(spans, spans[1:], strict=False):
        assert later[0] == earlier[1] + timedelta(days=1)


def test_a_single_day_is_one_window() -> None:
    assert _windows(DAY, DAY) == [(DAY, DAY)]


def test_a_backwards_range_asks_for_nothing() -> None:
    assert _windows(date(2026, 3, 1), date(2026, 1, 1)) == []


# --- what counts as a candle ----------------------------------------------


def test_a_row_outside_market_hours_is_not_stored() -> None:
    """A pre-open print would become a candle the backtester replays as if it
    had been tradeable."""
    pre_open = row(-30)
    assert _parse_candle(pre_open, TOKEN, 60, upstox_backfill.DEFAULT_TRADING_CALENDAR) is None


def test_a_malformed_row_is_skipped_rather_than_raising() -> None:
    """One bad row must not end an instrument's whole six months."""
    calendar = upstox_backfill.DEFAULT_TRADING_CALENDAR
    assert _parse_candle(["not-a-date", 1, 2, 3, 4, 5, 0], TOKEN, 60, calendar) is None
    assert _parse_candle([row(1)[0], "x", "y", "z", "w", 1, 0], TOKEN, 60, calendar) is None
    assert _parse_candle(row(1)[:3], TOKEN, 60, calendar) is None


def test_a_good_row_keeps_its_prices_exactly() -> None:
    candle = _parse_candle(row(1, price="2671.70"), TOKEN, 60, upstox_backfill.DEFAULT_TRADING_CALENDAR)
    assert candle is not None
    assert str(candle.open) == "2671.70"
    assert candle.closed_at - candle.opened_at == timedelta(seconds=60)


# --- storing --------------------------------------------------------------


async def test_storing_the_same_candles_twice_stores_them_once() -> None:
    """What makes the job resumable: a window that failed can be re-run without
    deciding first what is already there."""
    calendar = upstox_backfill.DEFAULT_TRADING_CALENDAR
    candles = [_parse_candle(row(minute), TOKEN, 60, calendar) for minute in range(5)]
    assert all(candles)
    assert await _store_batch(candles) == 5  # type: ignore[arg-type]
    assert await _store_batch(candles) == 0  # type: ignore[arg-type]
    async with SessionLocal() as session:
        held = await session.scalar(select(func.count(MarketCandle.id)).where(MarketCandle.instrument_token == TOKEN))
    assert held == 5


# --- the whole job --------------------------------------------------------


async def test_it_loads_a_range_and_reports_what_landed(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = FakeResponse({"data": {"candles": [row(minute) for minute in range(3)]}})
    client = install(monkeypatch, payload)

    report = await backfill_intraday_range(
        get_settings(),
        from_date=DAY,
        to_date=DAY,
        access_token="test-token",
        instruments=[TOKEN],
    )
    assert isinstance(report, RangeBackfillReport)
    assert report.stored[TOKEN] == 3
    assert report.sessions[TOKEN] == {DAY}
    assert not report.failures
    assert len(client.urls) == 1
    # The dated endpoint, not the intraday one that only ever returns today.
    assert f"/1minute/{DAY.isoformat()}/{DAY.isoformat()}" in client.urls[0]


async def test_a_second_run_over_the_same_range_adds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = FakeResponse({"data": {"candles": [row(minute) for minute in range(3)]}})
    install(monkeypatch, payload)
    settings = get_settings()
    first = await backfill_intraday_range(settings, from_date=DAY, to_date=DAY, access_token="t", instruments=[TOKEN])
    second = await backfill_intraday_range(settings, from_date=DAY, to_date=DAY, access_token="t", instruments=[TOKEN])
    assert first.stored[TOKEN] == 3
    assert second.stored[TOKEN] == 0
    assert second.already_present[TOKEN] == 3


async def test_it_writes_raw_candles_and_nothing_else(monkeypatch: pytest.MonkeyPatch) -> None:
    """The negative property, and the reason this does not call
    ``persist_completed``: that path recomputes indicators and can publish a
    snapshot the live scanner reads. Over six months of candles it would
    overwrite live state and bury the database in work mid-session."""
    payload = FakeResponse({"data": {"candles": [row(minute) for minute in range(20)]}})
    install(monkeypatch, payload)
    async with SessionLocal() as session:
        before = await session.scalar(select(func.count(MarketIndicatorSnapshot.id)))

    await backfill_intraday_range(get_settings(), from_date=DAY, to_date=DAY, access_token="t", instruments=[TOKEN])

    async with SessionLocal() as session:
        after = await session.scalar(select(func.count(MarketIndicatorSnapshot.id)))
        stored = await session.scalar(select(func.count(MarketCandle.id)).where(MarketCandle.instrument_token == TOKEN))
    assert stored == 20
    assert after == before


async def test_one_failed_window_does_not_end_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gap, not a crash. The failure is named so the operator knows which
    range to ask for again."""
    good = FakeResponse({"data": {"candles": [row(minute) for minute in range(2)]}})
    refused = FakeResponse({}, status_code=400)

    def answer(url: str):
        return refused if "2026-03-02" in url else good

    install(monkeypatch, answer)
    report = await backfill_intraday_range(
        get_settings(),
        from_date=date(2026, 3, 2),
        to_date=date(2026, 3, 27),
        access_token="t",
        instruments=[TOKEN],
        window_days=25,
    )
    assert report.failures[TOKEN]
    assert "HTTP 400" in report.failures[TOKEN][0]
    # The later window still ran.
    assert report.stored[TOKEN] == 2


async def test_a_network_error_is_recorded_and_the_next_instrument_still_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good = FakeResponse({"data": {"candles": [row(minute) for minute in range(2)]}})

    def answer(url: str):
        if TOKEN in url:
            return RuntimeError("connection reset")
        return good

    install(monkeypatch, answer)
    report = await backfill_intraday_range(
        get_settings(), from_date=DAY, to_date=DAY, access_token="t", instruments=[TOKEN, OTHER]
    )
    assert "connection reset" in report.failures[TOKEN][0]
    assert report.stored[TOKEN] == 0
    assert report.stored[OTHER] == 2


async def test_no_token_means_no_requests_rather_than_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    client = install(monkeypatch, FakeResponse({"data": {"candles": []}}))
    settings = get_settings()
    monkeypatch.setattr(settings, "upstox_access_token", None, raising=False)
    report = await backfill_intraday_range(settings, from_date=DAY, to_date=DAY, access_token=None, instruments=[TOKEN])
    assert report.total_stored == 0
    assert client.urls == []


async def test_a_year_the_calendar_has_not_confirmed_is_counted_not_silently_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The trap this job could have walked into.

    ``TradingCalendar`` fails closed for a year it has no confirmed holiday
    list for -- correct for live trading, and for a backfill it means a range
    reaching into that year stores nothing at all. Asking for twelve months
    today would have quietly loaded six and left a backtest reporting on half
    the history it was told it had. So the refusals are counted, and the
    operator is shown the number.
    """
    unconfirmed = date(2025, 3, 4)  # a Tuesday, but 2025 is not in the confirmed years
    payload = FakeResponse({"data": {"candles": [row(minute, day=unconfirmed) for minute in range(10)]}})
    install(monkeypatch, payload)

    report = await backfill_intraday_range(
        get_settings(),
        from_date=unconfirmed,
        to_date=unconfirmed,
        access_token="t",
        instruments=[TOKEN],
    )
    assert report.stored[TOKEN] == 0
    assert report.out_of_session[TOKEN] == 10
    assert not report.failures  # Upstox answered; we are the ones refusing.
