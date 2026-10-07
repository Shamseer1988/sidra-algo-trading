"""Saying how a live trade ended, every time one does.

On 7 October an IRCTC short was opened automatically, the operator got a
message saying so, and the stop filled an hour later in silence. The exit sweep
announces the exits *it* performs — a target, a square-off, a position it had to
flatten — and a stop resting at the broker is none of those. It fills at the
exchange, the position goes to zero, and the next sweep skips a flat row. The
single most common way a trade ends was the one way that produced no message.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.db.models import AuditLog, LiveOrderSubmission
from app.db.session import SessionLocal
from app.services import live_trade_alerts as alerts
from app.services.broker_adapter import BrokerOrderRecord

SIGNAL = uuid4()


def submission(
    *,
    order_type: str = "LIMIT",
    side: str = "SELL",
    numbers: list[str] | None = None,
    minutes_ago: int = 60,
    signal_id=SIGNAL,  # noqa: ANN001
    symbol: str = "IRCTC",
):
    """The real mapped class, because canonical_side and canonical_order_type
    are properties derived from the snapshot and a stand-in would answer for
    them whatever the code asked."""
    return LiveOrderSubmission(
        client_order_id=f"sidra-{uuid4().hex[:12]}",
        paper_signal_id=signal_id,
        broker="UPSTOX",
        exchange="NSE_EQ",
        trading_symbol=symbol,
        product="I",
        price_type="LMT",
        transaction_type="S",
        quantity=21,
        status="ACCEPTED",
        broker_order_numbers=list(numbers or []),
        created_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        request_snapshot={"canonical": {"side": side, "orderType": order_type, "product": "INTRADAY"}},
    )


def row(order_id: str, *, filled: int = 21, price: str = "450.94", status: str = "COMPLETE"):
    return BrokerOrderRecord(
        broker_order_id=order_id,
        client_order_id="sidra-1",
        status=status,
        symbol="IRCTC",
        side="SELL",
        order_type="LIMIT",
        quantity=21,
        filled_quantity=filled,
        average_price=Decimal(price),
    )


# --- the 7 October short, read out of the broker's book ----------------------


def the_irctc_day():
    """Entry sold 21 at 450.94; the stop bought them back at 454.45."""
    entry = submission(order_type="LIMIT", side="SELL", numbers=["261007000043355"], minutes_ago=90)
    stop = submission(order_type="SL-M", side="BUY", numbers=["261007000043999"], minutes_ago=89)
    book = [
        row("261007000043355", price="450.94"),
        row("261007000043999", price="454.45"),
    ]
    return [entry, stop], book


def test_a_stop_that_filled_at_the_broker_is_a_closed_trade():
    submissions, book = the_irctc_day()
    closed = alerts.closed_trades(submissions, book)

    assert len(closed) == 1
    trade = closed[0]
    assert (trade.symbol, trade.side, trade.quantity) == ("IRCTC", "SHORT", 21)
    assert (trade.entry_price, trade.exit_price) == (Decimal("450.94"), Decimal("454.45"))
    # A short loses when it buys back higher: (450.94 - 454.45) x 21.
    assert trade.gross == Decimal("-73.71")
    assert trade.ending == alerts.BY_STOP
    assert trade.won is False


def test_a_long_that_reached_its_target_is_read_the_other_way_round():
    entry = submission(order_type="LIMIT", side="BUY", numbers=["e1"], minutes_ago=90)
    exit_order = submission(order_type="MARKET", side="SELL", numbers=["x1"], minutes_ago=30)
    book = [row("e1", price="100.00"), row("x1", price="110.00", filled=10)]

    trade = alerts.closed_trades([entry, exit_order], book)[0]
    assert trade.side == "LONG"
    assert trade.gross == Decimal("100.00")
    assert trade.ending == alerts.BY_EXIT
    assert trade.won is True


def test_an_entry_that_has_not_been_closed_is_not_a_closed_trade():
    entry = submission(order_type="LIMIT", numbers=["e1"])
    assert alerts.closed_trades([entry], [row("e1")]) == []


def test_an_unfilled_exit_is_not_a_closed_trade():
    """A resting stop is not a finished trade, however long it rests."""
    submissions, book = the_irctc_day()
    book[1] = row("261007000043999", filled=0, status="OPEN")
    assert alerts.closed_trades(submissions, book) == []


def test_an_entry_that_never_filled_closes_nothing():
    submissions, book = the_irctc_day()
    book[0] = row("261007000043355", filled=0, status="REJECTED")
    assert alerts.closed_trades(submissions, book) == []


def test_an_order_the_book_does_not_know_is_skipped():
    submissions, _ = the_irctc_day()
    assert alerts.closed_trades(submissions, []) == []


def test_two_trades_in_one_stock_are_two_closed_trades():
    """Signals keep them apart; a symbol cannot."""
    first, second = uuid4(), uuid4()
    submissions = [
        submission(order_type="LIMIT", side="BUY", numbers=["a1"], minutes_ago=120, signal_id=first),
        submission(order_type="SL-M", side="SELL", numbers=["a2"], minutes_ago=119, signal_id=first),
        submission(order_type="LIMIT", side="BUY", numbers=["b1"], minutes_ago=60, signal_id=second),
        submission(order_type="SL-M", side="SELL", numbers=["b2"], minutes_ago=59, signal_id=second),
    ]
    book = [row("a1", price="100"), row("a2", price="95"), row("b1", price="100"), row("b2", price="105")]

    closed = alerts.closed_trades(submissions, book)
    assert len(closed) == 2
    assert sorted(trade.gross for trade in closed) == [Decimal("-105.00"), Decimal("105.00")]


def test_a_trade_closed_in_two_fills_is_two_movements_of_money():
    """An operator told about one half and left silent about the other has been
    told something false."""
    entry = submission(order_type="LIMIT", side="BUY", numbers=["e1"], minutes_ago=90)
    first = submission(order_type="MARKET", side="SELL", numbers=["x1"], minutes_ago=40)
    second = submission(order_type="MARKET", side="SELL", numbers=["x2"], minutes_ago=30)
    book = [row("e1", price="100"), row("x1", price="110", filled=5), row("x2", price="112", filled=5)]

    closed = alerts.closed_trades([entry, first, second], book)
    assert [trade.exit_order_id for trade in closed] == ["x1", "x2"]
    assert sum(trade.gross for trade in closed) == Decimal("110.00")


# --- the message ------------------------------------------------------------


def test_the_message_names_how_the_trade_ended_and_what_it_cost():
    submissions, book = the_irctc_day()
    trade = alerts.closed_trades(submissions, book)[0]
    text = alerts.message(trade, date(2026, 10, 7), Decimal("-73.71"))

    assert "TRADE CLOSED" in text
    assert alerts.BY_STOP in text
    assert "IRCTC" in text
    assert "450.94" in text and "454.45" in text
    assert "73.71" in text
    assert "Account today" in text


def test_a_day_total_the_broker_would_not_report_is_left_out():
    submissions, book = the_irctc_day()
    trade = alerts.closed_trades(submissions, book)[0]
    assert "Account today" not in alerts.message(trade, date(2026, 10, 7), None)


# --- sent once, and only once -----------------------------------------------


async def clean() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(AuditLog).where(AuditLog.event_type == alerts.ALERT_EVENT))
        await session.commit()


@pytest.fixture(autouse=True)
async def reset():
    await clean()
    yield
    await clean()


async def test_a_trade_is_announced_once_and_then_never_again() -> None:
    """A restart, a second sweep or a re-read of the same book must not produce
    a second message about the same money."""
    submissions, book = the_irctc_day()
    trades = alerts.closed_trades(submissions, book)

    async with SessionLocal() as session:
        assert await alerts.unannounced(session, trades) == trades
        await alerts.record_announced(session, trades[0])
        await session.commit()

    async with SessionLocal() as session:
        assert await alerts.unannounced(session, trades) == []


async def test_the_ledger_entry_says_what_it_was_about() -> None:
    submissions, book = the_irctc_day()
    trade = alerts.closed_trades(submissions, book)[0]
    async with SessionLocal() as session:
        await alerts.record_announced(session, trade)
        await session.commit()

    async with SessionLocal() as session:
        stored = (await session.scalars(select(AuditLog).where(AuditLog.event_type == alerts.ALERT_EVENT))).one()
    assert stored.metadata_json["symbol"] == "IRCTC"
    assert stored.metadata_json["ending"] == alerts.BY_STOP
    assert stored.metadata_json["exit_order_id"] == "261007000043999"


async def test_nothing_closed_asks_the_ledger_nothing() -> None:
    async with SessionLocal() as session:
        assert await alerts.unannounced(session, []) == []
