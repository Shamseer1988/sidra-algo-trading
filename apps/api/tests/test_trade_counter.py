"""Four trades a day — counted the way an operator means it.

The ceiling used to count signals: rows in paper_signals that risk had not
rejected. A signal whose entry order never filled — the limit was never touched,
or the order was cancelled at the cutoff — spent a quarter of the day's budget
on a trade that did not happen.

Each test below is one clause of the rule Shamseer specified, and each exists
because the old counter got that clause wrong.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.db.models import LiveOrderSubmission, PaperFill, PaperOrder, PaperSignal
from app.db.session import SessionLocal
from app.services.trade_counter import count_filled_entries, session_bounds_utc

SESSION = date(2026, 9, 24)
TOKEN = "NSE_EQ|INECOUNTER"


async def clean() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(PaperFill))
        await session.execute(delete(PaperOrder).where(PaperOrder.instrument_token == TOKEN))
        await session.execute(delete(PaperSignal).where(PaperSignal.instrument_token == TOKEN))
        start, end = session_bounds_utc(SESSION)
        await session.execute(
            delete(LiveOrderSubmission).where(
                LiveOrderSubmission.created_at >= start, LiveOrderSubmission.created_at < end
            )
        )
        await session.commit()


@pytest.fixture(autouse=True)
async def reset():
    await clean()
    yield
    await clean()


async def new_signal() -> PaperSignal:
    async with SessionLocal() as session:
        signal = PaperSignal(
            signal_key=f"count-{uuid4()}",
            instrument_token=TOKEN,
            session_date=SESSION,
            candle_opened_at=datetime(2026, 9, 24, 4, 0, tzinfo=UTC),
            strategy_version="orb-retest-v1@1",
            side="LONG",
            entry_price=Decimal("100"),
            stop_price=Decimal("98"),
            target_price=Decimal("103"),
            quantity=10,
            risk_amount=Decimal("20"),
            score=90,
            score_breakdown={},
            strategy_snapshot={},
            indicator_snapshot={},
        )
        session.add(signal)
        await session.commit()
        await session.refresh(signal)
        return signal


async def order(*, role: str = "ENTRY", filled: int = 0, status: str = "PENDING") -> PaperOrder:
    signal = await new_signal()
    async with SessionLocal() as session:
        record = PaperOrder(
            paper_signal_id=signal.id,
            client_order_id=f"count-{uuid4()}",
            instrument_token=TOKEN,
            session_date=SESSION,
            strategy_version="orb-retest-v1@1",
            side="BUY",
            order_type="MARKET",
            order_role=role,
            quantity=10,
            filled_quantity=filled,
            status=status,
            eligible_after=datetime(2026, 9, 24, 4, 0, tzinfo=UTC),
        )
        session.add(record)
        await session.commit()
        await session.refresh(record)
        return record


async def live_submission(*, status: str, when: datetime | None = None) -> None:
    async with SessionLocal() as session:
        session.add(
            LiveOrderSubmission(
                client_order_id=f"sidra-{uuid4().hex[:12]}",
                broker="UPSTOX",
                exchange="NSE_EQ",
                trading_symbol=TOKEN,
                product="I",
                price_type="LIMIT",
                transaction_type="BUY",
                quantity=10,
                price=Decimal("100"),
                status=status,
                created_at=when or datetime(2026, 9, 24, 5, 0, tzinfo=UTC),
            )
        )
        await session.commit()


async def count() -> int:
    async with SessionLocal() as session:
        return (await count_filled_entries(session, SESSION)).total


# --- what counts ----------------------------------------------------------


async def test_a_filled_entry_counts_once() -> None:
    await order(filled=10, status="FILLED")
    assert await count() == 1


async def test_a_partially_filled_entry_counts_once_not_by_fill() -> None:
    """A position that exists is a trade, and waiting for the remainder before
    counting it would let a second entry through beside it."""
    await order(filled=3, status="PARTIALLY_FILLED")
    assert await count() == 1


async def test_further_fills_of_the_same_entry_do_not_add_to_the_count() -> None:
    record = await order(filled=3, status="PARTIALLY_FILLED")
    async with SessionLocal() as session:
        stored = await session.get(PaperOrder, record.id)
        stored.filled_quantity = 10
        stored.status = "FILLED"
        await session.commit()
    assert await count() == 1


# --- what does not count --------------------------------------------------


async def test_a_signal_on_its_own_counts_nothing() -> None:
    """The defect this replaces. Four signals used to be four trades."""
    for _ in range(4):
        await new_signal()
    assert await count() == 0


async def test_an_entry_that_never_filled_counts_nothing() -> None:
    await order(filled=0, status="PENDING")
    assert await count() == 0


async def test_a_rejected_entry_counts_nothing() -> None:
    await order(filled=0, status="REJECTED")
    assert await count() == 0


async def test_a_cancelled_entry_with_no_fill_counts_nothing() -> None:
    """The cutoff cancels working entries. That is not a trade taken."""
    await order(filled=0, status="CANCELLED")
    assert await count() == 0


@pytest.mark.parametrize("role", ["TARGET", "STOP", "HALT"])
async def test_an_exit_order_is_not_an_entry(role: str) -> None:
    """Otherwise every trade would count twice: once in, once out."""
    await order(role=role, filled=10, status="FILLED")
    assert await count() == 0


async def test_yesterdays_trades_do_not_spend_todays_budget() -> None:
    signal = await new_signal()
    async with SessionLocal() as session:
        stored = await session.get(PaperSignal, signal.id)
        stored.session_date = date(2026, 9, 23)
        session.add(
            PaperOrder(
                paper_signal_id=signal.id,
                client_order_id=f"count-{uuid4()}",
                instrument_token=TOKEN,
                session_date=date(2026, 9, 23),
                strategy_version="orb-retest-v1@1",
                side="BUY",
                order_type="MARKET",
                order_role="ENTRY",
                quantity=10,
                filled_quantity=10,
                status="FILLED",
                eligible_after=datetime(2026, 9, 23, 4, 0, tzinfo=UTC),
            )
        )
        await session.commit()
    assert await count() == 0


# --- account-wide, across brokers ----------------------------------------


async def test_paper_and_live_share_one_ceiling() -> None:
    """Four across every strategy and both brokers, not four of each."""
    await order(filled=10, status="FILLED")
    await live_submission(status="ACCEPTED")
    assert await count() == 2


async def test_an_order_recovered_from_an_unknown_still_counts() -> None:
    """It exists at the exchange. Excluding it would let a lost response buy a
    trade the account already took."""
    await live_submission(status="RESOLVED_PLACED")
    assert await count() == 1


@pytest.mark.parametrize("status", ["PREPARED", "REJECTED", "UNKNOWN", "NEEDS_REVIEW"])
async def test_a_live_order_the_broker_did_not_place_counts_nothing(status: str) -> None:
    await live_submission(status=status)
    assert await count() == 0


# --- the IST day boundary -------------------------------------------------


async def test_the_session_day_runs_on_ist_not_utc() -> None:
    """A 09:20 IST order is 03:50 UTC the same day; a 23:00 IST one is the next
    UTC day. Bucketing on UTC would move trades between sessions."""
    start, end = session_bounds_utc(SESSION)
    assert start.isoformat() == "2026-09-23T18:30:00+00:00"
    assert end.isoformat() == "2026-09-24T18:30:00+00:00"


async def test_an_order_just_inside_the_ist_day_counts() -> None:
    start, _ = session_bounds_utc(SESSION)
    await live_submission(status="ACCEPTED", when=start)
    assert await count() == 1


async def test_an_order_just_before_the_ist_day_does_not() -> None:
    start, _ = session_bounds_utc(SESSION)
    await live_submission(status="ACCEPTED", when=start - timedelta(seconds=1))
    assert await count() == 0


async def test_an_order_at_the_end_boundary_belongs_to_the_next_day() -> None:
    """Half-open, so a trade cannot be counted in two sessions."""
    _, end = session_bounds_utc(SESSION)
    await live_submission(status="ACCEPTED", when=end)
    assert await count() == 0


# --- what the operator is told -------------------------------------------


async def test_the_explanation_names_where_the_budget_went() -> None:
    """ "Ceiling reached" without numbers sends them to the database."""
    await order(filled=10, status="FILLED")
    await live_submission(status="ACCEPTED")
    async with SessionLocal() as session:
        taken = await count_filled_entries(session, SESSION)
    detail = taken.explain(4)
    assert "2 of 4 used" in detail
    assert "1 paper entry filled" in detail
    assert "1 live order placed" in detail
    # The gap is stated rather than hidden: live fills are not tracked yet.
    assert "did not fill" in detail


async def test_remaining_never_goes_negative() -> None:
    for _ in range(3):
        await order(filled=10, status="FILLED")
    async with SessionLocal() as session:
        taken = await count_filled_entries(session, SESSION)
    assert taken.remaining(2) == 0


async def test_an_empty_day_says_so() -> None:
    async with SessionLocal() as session:
        taken = await count_filled_entries(session, SESSION)
    assert taken.total == 0
    assert "nothing filled yet" in taken.explain(4)
