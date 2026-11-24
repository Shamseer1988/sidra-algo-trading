"""The scanner's day, summed up and paged, against a real database."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.db.models import ScannerEvaluation
from app.db.session import SessionLocal
from app.services import scanner_report

DAY = date(2031, 3, 3)
EARLIER = date(2031, 3, 2)
START = datetime(2031, 3, 3, 4, 15, tzinfo=UTC)
RELIANCE = "NSE_EQ|INE002A01018"
SBIN = "NSE_EQ|INE062A01020"


async def clean() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(ScannerEvaluation).where(ScannerEvaluation.session_date.in_((DAY, EARLIER))))
        await session.commit()
    scanner_report._cache.clear()


@pytest.fixture(autouse=True)
async def reset():
    await clean()
    yield
    await clean()


async def check(
    minute=0, *, token=RELIANCE, strategy="rs-pullback-default", status="WATCHING", score=0, reason="Awaiting", day=DAY
):
    async with SessionLocal() as session:
        session.add(
            ScannerEvaluation(
                evaluation_key=uuid4().hex,
                instrument_token=token,
                session_date=day,
                candle_opened_at=START + timedelta(minutes=minute),
                strategy_id=strategy,
                strategy_name="RS Pullback" if strategy.startswith("rs") else strategy,
                strategy_version=1,
                status=status,
                decision_state="AWAITING",
                side="LONG" if score else None,
                reason=reason,
                candle_close=Decimal("100") + minute,
                score=score,
            )
        )
        await session.commit()


async def a_day() -> None:
    await check(0, status="ACCEPTED", score=80, reason="Paper signal confirmed")
    await check(1, score=70, status="REJECTED", reason="Reclaim failed score threshold")
    await check(2, token=SBIN, score=65, status="REJECTED", reason="Reclaim failed score threshold")
    await check(3, token=SBIN)
    await check(4, token=SBIN, strategy="data-quality", status="REJECTED", reason="5 candles are missing")
    await check(5, reason="Outside configured trade window", status="REJECTED")
    await check(5, token=SBIN, reason="Outside configured trade window", status="REJECTED")


async def test_the_day_is_counted_and_the_clock_is_kept_apart_from_the_reasons():
    await a_day()
    async with SessionLocal() as session:
        report = await scanner_report.build(session, DAY)
    assert report["totals"] | {"last_candle": None} == {
        "checks": 7,
        "outside_hours": 2,
        "blocked_by_data": 1,
        "scored": 3,
        "accepted": 1,
        "shares": 2,
        "last_candle": None,
    }
    assert report["reasons"] == [
        {"reason": "Reclaim failed score threshold", "count": 2},
        {"reason": "Awaiting", "count": 1},
    ]
    # The gate is not a strategy, and is not listed as one.
    assert [row["strategy_id"] for row in report["strategies"]] == ["rs-pullback-default"]
    assert report["strategies"][0]["best_score"] == 80


async def test_near_misses_are_best_first_and_never_include_what_was_taken():
    await a_day()
    async with SessionLocal() as session:
        report = await scanner_report.build(session, DAY)
    assert [row["score"] for row in report["near"]] == [70, 65]
    assert [row["score"] for row in report["signals"]] == [80]


async def test_each_share_carries_its_best_moment_and_the_last_close():
    await a_day()
    async with SessionLocal() as session:
        report = await scanner_report.build(session, DAY)
    first, second = report["shares"]
    assert (first["instrument_token"], first["accepted"], first["best"]["score"]) == (RELIANCE, 1, 80)
    assert (second["instrument_token"], second["blocked"], second["best"]["score"]) == (SBIN, 1, 65)
    assert second["last_close"] == 105
    assert first["script_name"] == "RELIANCE"


async def test_a_day_with_no_checks_is_empty_rather_than_an_error():
    async with SessionLocal() as session:
        report = await scanner_report.build(session, DAY)
    assert report["totals"]["checks"] == 0
    assert report["shares"] == []


async def test_sessions_are_newest_first():
    await check(day=EARLIER)
    await check(day=DAY)
    async with SessionLocal() as session:
        found = await scanner_report.recent_sessions(session, limit=2)
    assert found == [DAY, EARLIER]


async def test_pages_cover_every_row_once_even_when_they_tie():
    for _ in range(7):
        await check(0, score=50)
    seen = []
    async with SessionLocal() as session:
        for offset in (0, 3, 6):
            total, rows = await scanner_report.page(session, scanner_report.Filters(DAY, by_score=True), 3, offset)
            assert total == 7
            seen += [row.id for row in rows]
    assert len(seen) == len(set(seen)) == 7


async def test_the_page_filters_are_applied_by_the_database():
    await a_day()

    async def total(**filters) -> int:
        async with SessionLocal() as session:
            return (await scanner_report.page(session, scanner_report.Filters(DAY, **filters), 10, 0))[0]

    assert await total() == 7
    assert await total(search="sbin") == 4
    assert await total(search="no such share") == 0
    assert await total(status="ACCEPTED") == 1
    assert await total(scored_only=True) == 3
    assert await total(hide_outside=True) == 5
    assert await total(strategy_id="data-quality") == 1
