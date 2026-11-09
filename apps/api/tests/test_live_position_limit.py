"""Counting a position the broker's book cannot show yet.

``maximum_open_positions`` read the broker's position book, which is the right
source for everything it can see and blind to exactly one thing: an order that
has left and whose fill has not come back. On 9 November that blindness took
four positions on a limit of two -- SBIN, PNB, BANKBARODA and VEDL, all entered
at 09:37:01, all held together until 12:10, three of them banks whose stops do
not fail independently.

So the count is the book plus what is in flight. The clauses that matter are
the ones that stop it over-counting, because the cost of this gate refusing
wrongly is a trade not taken every time it happens:

  * an in-flight instrument that has reached the book is not counted twice;
  * an entry whose limit is never touched must not hold a slot all day;
  * a protective stop and an exit are not entries and hold nothing;
  * exposure stays the book's own figure, because an unfilled order has no
    honest amount of money to add.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.db.models import LiveOrderSubmission
from app.db.session import SessionLocal
from app.services.live_risk import IN_FLIGHT_WINDOW_SECONDS, live_entries_in_flight
from app.services.trade_counter import session_bounds_utc

SESSION = date(2031, 4, 22)
SBIN = "NSE_EQ|INEPOSLIMIT01"
PNB = "NSE_EQ|INEPOSLIMIT02"
# Mid-session, so no reading of the day boundary moves it.
MIDDAY = datetime(2031, 4, 22, 5, 30, tzinfo=UTC)


async def clean() -> None:
    start, end = session_bounds_utc(SESSION)
    async with SessionLocal() as session:
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


async def submission(
    *,
    token: str = SBIN,
    status: str = "ACCEPTED",
    planned_risk: str | None = "125.00",
    fill_seen: bool = False,
    when: datetime | None = None,
    snapshot: dict | None = None,
) -> None:
    """One live submission. ``planned_risk`` is what makes it an entry."""
    created = when or MIDDAY
    async with SessionLocal() as session:
        session.add(
            LiveOrderSubmission(
                client_order_id=f"sidra-{uuid4().hex[:16]}",
                broker="UPSTOX",
                exchange="NSE_EQ",
                trading_symbol=token,
                product="I",
                price_type="LIMIT",
                transaction_type="BUY",
                quantity=10,
                price=Decimal("100"),
                planned_risk=Decimal(planned_risk) if planned_risk is not None else None,
                status=status,
                created_at=created,
                # instrument_token is read back from here, not from a column:
                # trading_symbol holds what the broker was asked for and the
                # brokers disagree about what that is.
                request_snapshot={"canonical": {"instrumentToken": token}} if snapshot is None else snapshot,
                fill_seen_at=created + timedelta(seconds=4) if fill_seen else None,
            )
        )
        await session.commit()


async def in_flight(*, now: datetime | None = None) -> tuple[str, ...]:
    async with SessionLocal() as session:
        return await live_entries_in_flight(session, SESSION, now=now or MIDDAY + timedelta(seconds=10))


# --- what is in flight ----------------------------------------------------


async def test_an_entry_with_no_fill_reported_is_in_flight() -> None:
    """The 09:37:01 case: the order has left and the book cannot show it."""
    await submission()
    assert await in_flight() == (SBIN,)


@pytest.mark.parametrize("status", ["PREPARED", "ACCEPTED", "UNKNOWN", "NEEDS_REVIEW"])
async def test_every_committed_status_is_in_flight(status: str) -> None:
    """PREPARED above all: it is written and committed before the request
    leaves, which is the moment the book is blindest."""
    await submission(status=status)
    assert await in_flight() == (SBIN,)


async def test_an_entry_the_broker_refused_is_not_in_flight() -> None:
    await submission(status="REJECTED")
    assert await in_flight() == ()


# --- what it must not count ----------------------------------------------


async def test_an_entry_whose_fill_was_reported_is_not_in_flight() -> None:
    """Once the fill is seen the position book shows it, and counting it here
    as well would hold two slots for one position."""
    await submission(fill_seen=True)
    assert await in_flight() == ()


async def test_a_protective_stop_holds_no_slot() -> None:
    """planned_risk is null on a stop and on an exit: they risk nothing new.

    This is also why a stricter count here cannot leave a position unprotected
    -- but the stop must not be counted as a position of its own either, or
    every entry would consume two slots the moment its stop was placed.
    """
    await submission(planned_risk=None)
    assert await in_flight() == ()


async def test_an_entry_older_than_the_window_holds_no_slot() -> None:
    """An entry limit that is never touched must not hold a slot for the rest
    of the day. Past the window the book is the whole truth again: either it
    filled and the book shows it, or it did not and there is nothing to show.
    """
    await submission()
    later = MIDDAY + timedelta(seconds=IN_FLIGHT_WINDOW_SECONDS + 1)
    assert await in_flight(now=later) == ()


async def test_yesterdays_entry_is_not_in_flight_today() -> None:
    """The window is applied inside the session's own bounds, not instead of
    them -- a day that starts with an open window would read the previous
    evening's orders."""
    await submission()
    async with SessionLocal() as session:
        tomorrow = await live_entries_in_flight(
            session, SESSION + timedelta(days=1), now=MIDDAY + timedelta(seconds=10)
        )
    assert tomorrow == ()


async def test_a_row_with_no_canonical_token_is_skipped_rather_than_guessed() -> None:
    """It cannot be matched against a position book either way, and guessing
    would double-count the position it may already be."""
    await submission(snapshot={})
    assert await in_flight() == ()


# --- several ---------------------------------------------------------------


async def test_two_shares_in_flight_are_two() -> None:
    await submission(token=SBIN)
    await submission(token=PNB)
    assert sorted(await in_flight()) == sorted((SBIN, PNB))


async def test_two_entries_on_one_share_are_one_instrument() -> None:
    """This gate counts positions, and two entries on one share are one
    company's position. How many trades it may take there is the per-share
    ceiling's question."""
    await submission(token=SBIN)
    await submission(token=SBIN)
    assert await in_flight() == (SBIN,)
