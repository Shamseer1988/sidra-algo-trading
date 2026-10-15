"""A traded day with no snapshot at all is still outstanding.

14 October had five live submissions, two completed round trips, and no broker
figures of any kind. 13 October had its charges only because an operator ran
the backfill by hand.

The mechanism: the 18:00 pass asks the broker for today, and Upstox's P&L
report returns nothing for a session it has not settled, so ``sync_days``
writes no row. The 08:40 catch-up then asks "which days have a row whose
charges have not settled", finds nothing for that date because there is no row,
concludes nothing is outstanding and makes no request. Nothing ever goes back,
so the day's charges are lost unless somebody notices.

That matters beyond tidiness: the charges are the difference between a gross
figure and what the account actually made, and a day missing them is a day
whose net P&L this system cannot state.
"""

import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.db.models import BrokerDaySnapshot, LiveOrderSubmission
from app.db.session import SessionLocal
from app.services.broker_day_figures import (
    UPSTOX,
    dates_awaiting_charges,
    dates_missing_snapshot,
)
from app.services.trade_counter import session_bounds_utc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TRADED = date(2095, 10, 14)
QUIET = date(2095, 10, 13)
# 09:40 IST on TRADED, which is 04:10 UTC.
MORNING = datetime(2095, 10, 14, 4, 10, tzinfo=UTC)
# 00:30 IST the same calendar day in IST, 19:00 UTC the day before. Stored this
# way it must still file under TRADED, not under the previous session.
AFTER_MIDNIGHT_UTC = datetime(2095, 10, 13, 19, 0, tzinfo=UTC)
WINDOW = (date(2095, 10, 8), date(2095, 10, 15))


async def submission(*, when: datetime, status: str = "ACCEPTED") -> None:
    async with SessionLocal() as session:
        session.add(
            LiveOrderSubmission(
                client_order_id=f"sidra-{uuid4().hex[:12]}",
                broker="UPSTOX",
                exchange="NSE_EQ",
                trading_symbol="HDFCBANK-EQ",
                product="I",
                price_type="LIMIT",
                transaction_type="BUY",
                quantity=15,
                price=Decimal("717.85"),
                status=status,
                created_at=when,
            )
        )
        await session.commit()


async def snapshot(day: date, *, charges: str | None) -> None:
    async with SessionLocal() as session:
        session.add(
            BrokerDaySnapshot(
                session_date=day,
                broker=UPSTOX,
                source="upstox:trade/profit-loss",
                realized_pnl=Decimal("-15.60"),
                charges=Decimal(charges) if charges is not None else None,
                turnover=Decimal("21359.60"),
                trade_count=1,
                payload={},
                fetched_at=datetime.now(UTC),
            )
        )
        await session.commit()


async def clean() -> None:
    async with SessionLocal() as session:
        for day in (TRADED, QUIET):
            start, end = session_bounds_utc(day)
            await session.execute(
                delete(LiveOrderSubmission).where(
                    LiveOrderSubmission.created_at >= start, LiveOrderSubmission.created_at < end
                )
            )
        await session.execute(
            delete(BrokerDaySnapshot).where(
                BrokerDaySnapshot.session_date >= WINDOW[0], BrokerDaySnapshot.session_date <= WINDOW[1]
            )
        )
        await session.commit()


@pytest.fixture(autouse=True)
async def reset():
    await clean()
    yield
    await clean()


async def missing() -> set[date]:
    async with SessionLocal() as session:
        return await dates_missing_snapshot(session, *WINDOW)


async def awaiting() -> set[date]:
    async with SessionLocal() as session:
        return await dates_awaiting_charges(session, *WINDOW)


# --- the 14 October case --------------------------------------------------


async def test_a_traded_day_with_no_snapshot_is_outstanding() -> None:
    """The regression. The old gate alone saw nothing to do here."""
    await submission(when=MORNING)
    assert await missing() == {TRADED}
    assert await awaiting() == set()


async def test_a_day_whose_snapshot_lacks_charges_is_not_double_counted() -> None:
    """The old gate already covers it; this one must not claim it too."""
    await submission(when=MORNING)
    await snapshot(TRADED, charges=None)
    assert await missing() == set()
    assert await awaiting() == {TRADED}


async def test_a_settled_day_is_outstanding_under_neither_gate() -> None:
    await submission(when=MORNING)
    await snapshot(TRADED, charges="12.20")
    assert await missing() == set()
    assert await awaiting() == set()


async def test_a_day_we_placed_nothing_on_is_not_outstanding() -> None:
    """A paper-only day has no broker charges to go looking for."""
    assert await missing() == set()


# --- what counts as having traded -----------------------------------------


async def test_an_order_that_never_reached_the_broker_does_not_make_a_day_outstanding() -> None:
    """PREPARED was written before the request left; REJECTED came back no.

    Neither produces a charge, and treating them as a traded day would make the
    morning pass spend a request on every day an order was refused.
    """
    for status in ("PREPARED", "REJECTED", "FAILED", "UNKNOWN"):
        await submission(when=MORNING, status=status)
    assert await missing() == set()


async def test_a_recovered_order_counts_as_having_traded() -> None:
    """An UNKNOWN that recovery proved had reached the broker cost charges."""
    await submission(when=MORNING, status="RESOLVED_PLACED")
    assert await missing() == {TRADED}


async def test_a_submission_stored_before_midnight_utc_files_under_its_ist_day() -> None:
    """19:00 UTC is 00:30 IST the next morning.

    Deriving the date from the UTC timestamp would file it under the previous
    session and leave the real one unfetched.
    """
    await submission(when=AFTER_MIDNIGHT_UTC)
    assert await missing() == {TRADED}


async def test_a_day_outside_the_window_is_not_returned() -> None:
    """The morning pass looks back a few sessions, not forever."""
    await submission(when=MORNING)
    async with SessionLocal() as session:
        assert await dates_missing_snapshot(session, date(2095, 10, 15), date(2095, 10, 15)) == set()
