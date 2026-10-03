"""What the broker filled, against what we asked for.

This seam exists because of a number on an operator's screen. The P&L calendar
said a day made ₹146.34; the Upstox app said ₹166.93. Both were right about
what they measured -- the rows labelled LIVE were carrying the *simulator's*
fill prices, because nothing had ever written a broker fill back. On one trade
the two differed by ₹1.42 a share.

So the tests here are mostly about arithmetic and about absence: a fill that was
not reported must not become a fill of zero, and half a round trip must not
become a result.
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.db.models import LiveOrderSubmission, PaperSignal
from app.db.session import SessionLocal
from app.services.broker_adapter import BrokerOrderRecord
from app.services.live_fills import FillSide, SignalFills, fills_by_signal, record_fills

NOW = datetime(2026, 10, 1, 4, 7, tzinfo=UTC)


def _today_ist():
    from app.services.trading_calendar import MARKET_TIMEZONE

    return datetime.now(UTC).astimezone(MARKET_TIMEZONE).date()


def submission(*, client_order_id="sidra-1", numbers=None, side=None, **kwargs):
    row = LiveOrderSubmission(
        client_order_id=client_order_id,
        broker="UPSTOX",
        exchange="NSE",
        trading_symbol="BHARTIARTL",
        product="I",
        price_type="MARKET",
        transaction_type="SELL",
        quantity=67,
        status="ACCEPTED",
        broker_order_numbers=numbers if numbers is not None else [],
        request_snapshot={"canonical": {"side": side}} if side else {},
        **kwargs,
    )
    return row


def order(*, broker_order_id="2610010001", client_order_id=None, filled=67, price="183.56", status="COMPLETE"):
    return BrokerOrderRecord(
        broker_order_id=broker_order_id,
        client_order_id=client_order_id,
        status=status,
        symbol="BHARTIARTL",
        side="SELL",
        order_type="MARKET",
        quantity=67,
        filled_quantity=filled,
        average_price=None if price is None else Decimal(price),
        placed_at="2026-10-01T04:07:01Z",
    )


# --- matching -------------------------------------------------------------


def test_a_fill_is_matched_by_the_broker_order_number():
    row = submission(numbers=["2610010001"])
    assert record_fills([row], [order()], now=NOW) == 1
    assert row.filled_quantity == 67
    assert row.average_fill_price == Decimal("183.5600")
    assert row.fill_seen_at == NOW


def test_a_fill_is_matched_by_our_own_tag_when_the_number_was_never_learned():
    """The case the seam exists to recover: a submission whose outcome was lost.

    The client order id is carried to the broker in a field we choose, which
    makes it the only key available when the broker's own number never came
    back to us.
    """
    row = submission(client_order_id="sidra-7f2c", numbers=[])
    assert record_fills([row], [order(client_order_id="sidra-7f2c")], now=NOW) == 1
    assert row.filled_quantity == 67


def test_somebody_elses_order_is_not_our_fill():
    row = submission(client_order_id="sidra-7f2c", numbers=["2610010001"])
    assert record_fills([row], [order(broker_order_id="9999", client_order_id=None)], now=NOW) == 0
    assert row.filled_quantity is None


# --- the arithmetic -------------------------------------------------------


def test_a_sliced_order_is_summed_and_weighted_by_quantity():
    """A broker slices an order above the freeze quantity.

    A plain mean of the slice prices would weight a 5-share slice like a
    500-share one, which is wrong by more the more unevenly it filled.
    """
    row = submission(numbers=["a", "b"])
    book = [
        order(broker_order_id="a", filled=60, price="183.00"),
        order(broker_order_id="b", filled=7, price="190.00"),
    ]
    record_fills([row], book, now=NOW)
    assert row.filled_quantity == 67
    # (60×183 + 7×190) / 67 = 183.7313...
    assert row.average_fill_price == Decimal("183.7313")


def test_a_quantity_without_a_price_records_the_quantity_and_no_price():
    """A price of zero on a short is an unbounded profit. It must stay absent."""
    row = submission(numbers=["a"])
    record_fills([row], [order(broker_order_id="a", filled=67, price=None)], now=NOW)
    assert row.filled_quantity == 67
    assert row.average_fill_price is None


def test_an_unfilled_order_is_recorded_as_filling_nothing():
    """Distinct from never having been looked at: a resting stop is a real state."""
    row = submission(numbers=["a"])
    record_fills([row], [order(broker_order_id="a", filled=0, price=None, status="OPEN")], now=NOW)
    assert row.filled_quantity == 0
    assert row.broker_status == "OPEN"


def test_slices_that_disagree_are_reported_as_the_disagreement():
    """Half filled and half rejected is neither COMPLETE nor REJECTED."""
    row = submission(numbers=["a", "b"])
    book = [
        order(broker_order_id="a", filled=60, status="COMPLETE"),
        order(broker_order_id="b", filled=0, price=None, status="REJECTED"),
    ]
    record_fills([row], book, now=NOW)
    assert row.broker_status == "PARTIAL"


# --- what it must not disturb ---------------------------------------------


def test_recording_a_fill_does_not_advance_our_own_status():
    """The exit manager finds a resting stop by ``status == ACCEPTED``.

    A stop advanced to the broker's word would not be found, would not be
    cancelled, and an exit sent past a live stop reverses a position instead of
    closing it. That failure has happened in this system once already.
    """
    row = submission(numbers=["a"])
    record_fills([row], [order(broker_order_id="a")], now=NOW)
    assert row.status == "ACCEPTED"
    assert row.broker_status == "COMPLETE"


def test_a_second_pass_over_the_same_book_changes_nothing():
    """Reconciliation runs on a loop; a rewrite per pass would be noise."""
    row = submission(numbers=["a"])
    assert record_fills([row], [order(broker_order_id="a")], now=NOW) == 1
    later = datetime(2026, 10, 1, 4, 9, tzinfo=UTC)
    assert record_fills([row], [order(broker_order_id="a")], now=later) == 0
    assert row.fill_seen_at == NOW


def test_a_changed_fill_is_picked_up():
    row = submission(numbers=["a"])
    record_fills([row], [order(broker_order_id="a", filled=30)], now=NOW)
    assert record_fills([row], [order(broker_order_id="a", filled=67)], now=NOW) == 1
    assert row.filled_quantity == 67


# --- a signal's round trip ------------------------------------------------


def test_the_same_arithmetic_serves_a_long_and_a_short():
    """A short sells first and buys back. That changes the order of events and
    not the subtraction, which is why there is one expression and not two."""
    long_trade = SignalFills(buy=FillSide(10, Decimal("100")), sell=FillSide(10, Decimal("110")))
    short_trade = SignalFills(buy=FillSide(10, Decimal("100")), sell=FillSide(10, Decimal("110")))
    assert long_trade.gross == Decimal("100.0000")
    assert short_trade.gross == Decimal("100.0000")


def test_entry_and_exit_depend_on_the_side():
    fills = SignalFills(buy=FillSide(10, Decimal("100")), sell=FillSide(10, Decimal("110")))
    assert fills.entry("LONG").price == Decimal("100")
    assert fills.exit("LONG").price == Decimal("110")
    assert fills.entry("SHORT").price == Decimal("110")
    assert fills.exit("SHORT").price == Decimal("100")


def test_only_the_quantity_that_round_tripped_counts():
    """An entry still open is not a result, and must not be priced as one."""
    fills = SignalFills(buy=FillSide(10, Decimal("100")), sell=FillSide(4, Decimal("110")))
    assert fills.matched_quantity == 4
    assert fills.gross == Decimal("40.0000")


def test_half_a_trade_has_no_gross_at_all():
    """Falling back whole is the point: a gross built from one known side and
    one unknown one would be a third number, true of nothing."""
    assert SignalFills(buy=FillSide(10, Decimal("100")), sell=FillSide()).gross is None
    assert SignalFills(buy=FillSide(10, None), sell=FillSide(10, Decimal("110"))).gross is None
    assert SignalFills().gross is None


# --- reading them back ----------------------------------------------------


@pytest.fixture(autouse=True)
async def clean():
    async with SessionLocal() as session:
        await session.execute(delete(LiveOrderSubmission))
        await session.execute(delete(PaperSignal))
        await session.commit()
    yield


async def _signal(session):
    signal = PaperSignal(
        signal_key=uuid4().hex,
        instrument_token="NSE_EQ|INE397D01024",
        session_date=NOW.date(),
        candle_opened_at=NOW,
        strategy_version="orb-retest-v1@3",
        side="SHORT",
        entry_price=Decimal("183"),
        stop_price=Decimal("185"),
        target_price=Decimal("180"),
        quantity=67,
        risk_amount=Decimal("100"),
        score=80,
    )
    session.add(signal)
    await session.flush()
    return signal


async def test_a_signals_fills_are_grouped_by_direction():
    async with SessionLocal() as session:
        signal = await _signal(session)
        session.add_all(
            [
                submission(
                    client_order_id="a",
                    side="SELL",
                    paper_signal_id=signal.id,
                    filled_quantity=67,
                    average_fill_price=Decimal("183.63"),
                ),
                submission(
                    client_order_id="b",
                    side="BUY",
                    paper_signal_id=signal.id,
                    filled_quantity=67,
                    average_fill_price=Decimal("180.84"),
                ),
            ]
        )
        await session.commit()
        fills = (await fills_by_signal(session, {signal.id}))[signal.id]

    assert fills.entry("SHORT").price == Decimal("183.6300")
    assert fills.exit("SHORT").price == Decimal("180.8400")
    assert fills.gross == Decimal("186.9300")


async def test_a_cancelled_stop_is_no_part_of_the_price():
    """It is part of the story of the trade and none of its arithmetic."""
    async with SessionLocal() as session:
        signal = await _signal(session)
        session.add_all(
            [
                submission(
                    client_order_id="a",
                    side="SELL",
                    paper_signal_id=signal.id,
                    filled_quantity=67,
                    average_fill_price=Decimal("183.63"),
                ),
                submission(
                    client_order_id="stop",
                    side="BUY",
                    paper_signal_id=signal.id,
                    filled_quantity=0,
                    average_fill_price=None,
                ),
                submission(
                    client_order_id="b",
                    side="BUY",
                    paper_signal_id=signal.id,
                    filled_quantity=67,
                    average_fill_price=Decimal("180.84"),
                ),
            ]
        )
        await session.commit()
        fills = (await fills_by_signal(session, {signal.id}))[signal.id]

    assert fills.buy.quantity == 67
    assert fills.gross == Decimal("186.9300")


async def test_a_submission_without_a_canonical_side_is_skipped():
    """``transaction_type`` holds the broker's word -- "B" at Firstock, "BUY" at
    Upstox. Reading it instead of the canonical side is the defect class that
    has cost this system four live failures."""
    async with SessionLocal() as session:
        signal = await _signal(session)
        session.add(
            submission(
                client_order_id="a", paper_signal_id=signal.id, filled_quantity=67, average_fill_price=Decimal("183.63")
            )
        )
        await session.commit()
        assert await fills_by_signal(session, {signal.id}) == {}


async def test_no_signals_asks_the_database_nothing():
    async with SessionLocal() as session:
        assert await fills_by_signal(session, set()) == {}


# --- the safety net -------------------------------------------------------


async def test_the_sweep_records_a_session_the_reconciler_never_saw(monkeypatch: pytest.MonkeyPatch):
    """Reconciliation only runs while armed and while the exchange is open.

    An activation that lapses at two o'clock would otherwise leave the
    afternoon's trades priced by the simulator permanently, which is the exact
    failure this seam exists to end.
    """
    from app.services import live_fills

    async with SessionLocal() as session:
        signal = await _signal(session)
        session.add(
            submission(client_order_id="sidra-late", side="SELL", numbers=["2610010001"], paper_signal_id=signal.id)
        )
        await session.commit()

    class Adapter:
        name = "UPSTOX"

        async def normalised_orders(self):
            return [order(broker_order_id="2610010001", filled=67, price="183.63")]

    async def report_adapter(_settings, _session):
        return Adapter()

    monkeypatch.setattr("app.services.live_execution_gateway.live_report_adapter", report_adapter)

    # The rows carry a database-set created_at, so the sweep has to be asked
    # for the IST date those rows actually landed on.
    async with SessionLocal() as session:
        changed = await live_fills.sweep_session_fills(object(), session, _today_ist())
    assert changed == 1

    async with SessionLocal() as session:
        row = await session.scalar(
            select(LiveOrderSubmission).where(LiveOrderSubmission.client_order_id == "sidra-late")
        )
        assert row.average_fill_price == Decimal("183.6300")


async def test_the_sweep_does_not_call_the_broker_for_a_day_with_no_submissions():
    """A paper deployment must not spend an API call proving it traded nothing."""
    from app.services import live_fills

    async with SessionLocal() as session:
        assert await live_fills.sweep_session_fills(object(), session, _today_ist()) == 0
