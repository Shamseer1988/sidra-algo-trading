"""Loading 630,000 candles must not cost a gigabyte of objects to discard.

The sweep refuses a run it estimates will not fit, because being killed by the
kernel an hour in is the worst outcome available. On 16 October a run passed
that check at an estimated 409 MB and was killed inside the first replay
anyway.

The estimate was right about the candles and silent about everything else.
``select(MarketCandle)`` builds a mapped instance per row and holds every one
of them -- in the result list and in the session's identity map -- while the
loop builds the CompletedCandle list beside it. Measured on this machine, an
entity row costs about 3,000 bytes against 300 for a streamed column row: on
630,432 candles that is roughly 1.9 GB of objects created and immediately
thrown away, on top of the 409 MB the replay actually keeps.

Selecting columns and streaming them makes the estimate honest, because the
only thing left alive is the thing it counts.
"""

import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from app.db.models import MarketCandle
from app.db.session import SessionLocal

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import run_sweep  # noqa: E402

TOKEN = "NSE_EQ|SWEEPLOAD"
DAY = date(2094, 6, 2)
OPENED = datetime(2094, 6, 2, 3, 45, tzinfo=UTC)


async def seed(count: int = 3) -> None:
    async with SessionLocal() as session:
        session.add_all(
            [
                MarketCandle(
                    instrument_token=TOKEN,
                    timeframe_seconds=60,
                    session_date=DAY,
                    opened_at=OPENED + timedelta(minutes=index),
                    closed_at=OPENED + timedelta(minutes=index + 1),
                    open=Decimal("100.25"),
                    high=Decimal("101.50"),
                    low=Decimal("99.75"),
                    close=Decimal("100.50"),
                    volume=1234,
                    tick_count=7,
                )
                for index in range(count)
            ]
        )
        await session.commit()


async def clean() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(MarketCandle).where(MarketCandle.instrument_token == TOKEN))
        await session.commit()


@pytest.fixture(autouse=True)
async def reset():
    await clean()
    yield
    await clean()


async def test_a_streamed_row_makes_the_same_candle_as_the_entity() -> None:
    """The one thing that would break the whole run, pinned.

    ``completed`` reads by attribute name, which a Row answers to as readily as
    a mapped instance -- so the mapping stays in one place. This fails the
    moment the selected columns stop covering what it reads.
    """
    await seed(1)
    async with SessionLocal() as session:
        entity = (await session.scalars(select(MarketCandle).where(MarketCandle.instrument_token == TOKEN))).one()
        stream = await session.stream(
            select(*run_sweep.CANDLE_COLUMNS)
            .where(MarketCandle.instrument_token == TOKEN)
            .execution_options(yield_per=run_sweep.CANDLE_BATCH)
        )
        rows = [row async for row in stream]

    assert run_sweep.completed(rows[0]) == run_sweep.completed(entity)


async def test_the_session_date_survives_without_being_selected() -> None:
    """CompletedCandle derives it from opened_at.

    _ranked_only groups by candle.session_date, so a load that could not
    produce it would drop every instrument from every session.
    """
    await seed(1)
    async with SessionLocal() as session:
        stream = await session.stream(select(*run_sweep.CANDLE_COLUMNS).where(MarketCandle.instrument_token == TOKEN))
        rows = [row async for row in stream]

    assert MarketCandle.session_date not in run_sweep.CANDLE_COLUMNS
    assert run_sweep.completed(rows[0]).session_date == DAY


async def test_every_candle_is_loaded_in_order() -> None:
    """Streaming in batches must not drop or reorder what the replay reads."""
    await seed(5)
    async with SessionLocal() as session:
        stream = await session.stream(
            select(*run_sweep.CANDLE_COLUMNS)
            .where(MarketCandle.instrument_token == TOKEN)
            .order_by(MarketCandle.instrument_token, MarketCandle.opened_at)
            .execution_options(yield_per=2)  # smaller than the row count, so it really batches
        )
        candles = [run_sweep.completed(row) async for row in stream]

    assert len(candles) == 5
    assert [candle.opened_at for candle in candles] == sorted(candle.opened_at for candle in candles)


def test_the_batch_size_is_set_and_sane() -> None:
    """yield_per is what makes this a stream rather than a slower fetch-all."""
    assert isinstance(run_sweep.CANDLE_BATCH, int)
    assert 100 <= run_sweep.CANDLE_BATCH <= 50_000
