"""The broker's own trades, read back out of the report already stored.

Every row here was downloaded with the day figures and kept whole in the
snapshot payload. Reading them costs nothing at the broker, which is what makes
it possible to show the trades behind a session whose local record no longer
exists.

What the rows are not is the part worth testing. They carry no stop, no target
and no net, because the first two are ours and the third cannot exist: Upstox
aggregates charges over a date range and publishes no per-trade figure. A row
that quietly answered zero to any of those would be read as a fact.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import delete

from app.db.models import BrokerDaySnapshot
from app.db.session import SessionLocal
from app.services import broker_trades

FIRST = date(2026, 10, 1)
SECOND = date(2026, 10, 5)


async def clean() -> None:
    async with SessionLocal() as session:
        await session.execute(
            delete(BrokerDaySnapshot).where(
                BrokerDaySnapshot.session_date >= FIRST, BrokerDaySnapshot.session_date <= SECOND
            )
        )
        await session.commit()


@pytest.fixture(autouse=True)
async def reset():
    await clean()
    yield
    await clean()


async def store(session_date, rows, *, broker="UPSTOX", charges="37.65"):
    async with SessionLocal() as session:
        session.add(
            BrokerDaySnapshot(
                session_date=session_date,
                broker=broker,
                source="profit-loss/data",
                realized_pnl=Decimal("166.93"),
                charges=None if charges is None else Decimal(charges),
                payload={"rows": rows},
            )
        )
        await session.commit()


def a_row(**overrides):
    row = {
        "scrip_name": "TATASTEEL",
        "isin": "INE081A01020",
        "trade_type": "INTRADAY",
        "quantity": 67,
        "buy_average": "183.63",
        "sell_average": "180.97",
        "buy_amount": "12303.21",
        "sell_amount": "12124.99",
    }
    row.update(overrides)
    return row


# --- reading one row -----------------------------------------------------


def read(row):
    from datetime import UTC, datetime

    return broker_trades.read_row(
        row, session_date=FIRST, broker="UPSTOX", fetched_at=datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    )


def test_a_row_carries_the_stock_the_size_and_both_prices():
    trade = read(a_row())
    assert trade.script_name == "TATASTEEL"
    assert trade.quantity == 67
    assert (trade.buy_price, trade.sell_price) == (Decimal("183.63"), Decimal("180.97"))


def test_gross_is_what_the_pair_sold_for_less_what_it_cost():
    """The report publishes the two amounts and no P&L column; the difference is
    how it expects to be read."""
    assert read(a_row()).gross_pnl == Decimal("-178.22")


def test_a_camelcase_response_reads_the_same_as_a_snake_case_one():
    """The REST responses are snake_case and the published SDKs are camelCase.
    Accepting one of the two silently empties every column."""
    trade = read({"scripName": "TATASTEEL", "buyAverage": "183.63", "buyAmount": "100", "sellAmount": "150"})
    assert trade.script_name == "TATASTEEL"
    assert trade.buy_price == Decimal("183.63")
    assert trade.gross_pnl == Decimal("50")


def test_a_pair_with_neither_amount_has_no_gross_rather_than_zero():
    trade = read(a_row(sell_amount=None, buy_amount=None))
    assert trade is not None and trade.gross_pnl is None


def test_a_half_known_pair_reads_the_same_way_the_day_total_reads_it():
    """One-sided rows are anomalous from Upstox, and the day total already
    treats the missing side as zero (``read_profit_loss``). Reading a row
    differently here would make the rows stop adding up to the day above them,
    which is a new disagreement rather than a fixed one."""
    from app.services.broker_day_figures import read_profit_loss

    row = a_row(sell_amount=None)
    realised, _, _ = read_profit_loss([row])
    assert read(row).gross_pnl == realised


def test_an_unpriced_leg_is_absent_rather_than_zero():
    trade = read(a_row(sell_average=None))
    assert trade.sell_price is None


def test_a_row_that_is_not_a_row_is_skipped():
    assert read("not a dict") is None
    assert read({}) is None


# --- reading a range -----------------------------------------------------


async def test_the_days_trades_come_back_without_asking_the_broker_anything():
    await store(FIRST, [a_row(), a_row(scrip_name="AXISBANK", quantity=10)])

    async with SessionLocal() as session:
        rows = await broker_trades.load_broker_trades(session, FIRST, FIRST)

    assert {row.script_name for row in rows} == {"TATASTEEL", "AXISBANK"}
    assert all(row.session_date == FIRST for row in rows)


async def test_only_the_newest_snapshot_of_a_day_is_read():
    """The older ones record how the broker's figures settled, not a second set
    of trades. Summing across them would count a re-fetched day twice."""
    await store(FIRST, [a_row(scrip_name="STALE")])
    await store(FIRST, [a_row(scrip_name="TATASTEEL")])

    async with SessionLocal() as session:
        rows = await broker_trades.load_broker_trades(session, FIRST, FIRST)

    assert [row.script_name for row in rows] == ["TATASTEEL"]


async def test_a_day_outside_the_range_is_not_read():
    await store(SECOND, [a_row()])
    async with SessionLocal() as session:
        assert await broker_trades.load_broker_trades(session, FIRST, FIRST) == []


async def test_one_brokers_rows_are_not_mixed_with_anothers():
    await store(FIRST, [a_row(scrip_name="TATASTEEL")], broker="UPSTOX")
    await store(FIRST, [a_row(scrip_name="ELSEWHERE")], broker="FIRSTOCK")

    async with SessionLocal() as session:
        rows = await broker_trades.load_broker_trades(session, FIRST, FIRST, broker="UPSTOX")

    assert [row.script_name for row in rows] == ["TATASTEEL"]


async def test_a_snapshot_with_no_rows_yields_nothing_rather_than_failing():
    await store(FIRST, [])
    async with SessionLocal() as session:
        assert await broker_trades.load_broker_trades(session, FIRST, FIRST) == []
