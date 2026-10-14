"""The day's live risk allocation, counted from live submissions.

``maximum_daily_risk_percent`` was enforced only by the paper risk engine, so
the live account's budget was policed by a simulation. These tests pin the
query that replaced it: what it counts, what it ignores, and that one trade
spends its risk once rather than once per order.

The figure is read from ``live_order_submissions.planned_risk`` and from
nothing else -- no paper table is joined, which is both the architectural rule
this codebase enforces and the reason the column exists.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.db.models import LiveOrderSubmission, PaperSignal
from app.db.session import SessionLocal
from app.services.live_risk import live_risk_allocated_today
from app.services.trade_counter import session_bounds_utc

SESSION = date(2026, 9, 24)
TOKEN = "NSE_EQ|INEALLOCATE"
# Inside SESSION's bounds, and late enough that no timezone reading of it lands
# on the previous day.
MIDDAY = datetime(2026, 9, 24, 5, 30, tzinfo=UTC)


async def clean() -> None:
    async with SessionLocal() as session:
        start, end = session_bounds_utc(SESSION)
        await session.execute(
            delete(LiveOrderSubmission).where(
                LiveOrderSubmission.created_at >= start, LiveOrderSubmission.created_at < end
            )
        )
        await session.execute(delete(PaperSignal).where(PaperSignal.instrument_token == TOKEN))
        await session.commit()


@pytest.fixture(autouse=True)
async def reset():
    await clean()
    yield
    await clean()


async def signal(risk: str) -> PaperSignal:
    async with SessionLocal() as session:
        record = PaperSignal(
            signal_key=f"alloc-{uuid4()}",
            instrument_token=TOKEN,
            session_date=SESSION,
            candle_opened_at=MIDDAY,
            strategy_version="orb-retest-v1@15",
            side="LONG",
            entry_price=Decimal("100"),
            stop_price=Decimal("98"),
            target_price=Decimal("103"),
            quantity=10,
            risk_amount=Decimal(risk),
            score=90,
            score_breakdown={},
            strategy_snapshot={},
            indicator_snapshot={},
        )
        session.add(record)
        await session.commit()
        await session.refresh(record)
        return record


async def submission(
    *,
    signal_id=None,
    status: str = "ACCEPTED",
    when: datetime | None = None,
    planned_risk: str | None = "86.08",
) -> None:
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
                paper_signal_id=signal_id,
                planned_risk=Decimal(planned_risk) if planned_risk is not None else None,
                created_at=when or MIDDAY,
            )
        )
        await session.commit()


async def allocated() -> tuple[Decimal, int]:
    async with SessionLocal() as session:
        return await live_risk_allocated_today(session, SESSION)


async def test_a_day_with_nothing_placed_has_allocated_nothing() -> None:
    assert await allocated() == (Decimal("0"), 0)


async def test_a_placed_order_allocates_the_risk_it_was_sized_from() -> None:
    await submission(signal_id=(await signal("86.08")).id, planned_risk="86.08")
    assert await allocated() == (Decimal("86.08"), 1)


async def test_two_trades_allocate_the_sum() -> None:
    await submission(signal_id=(await signal("86.08")).id, planned_risk="86.08")
    await submission(signal_id=(await signal("100.00")).id, planned_risk="100.00")
    total, trades = await allocated()
    assert (total, trades) == (Decimal("186.08"), 2)


async def test_one_trades_three_orders_allocate_its_risk_once() -> None:
    """The entry, the protective stop behind it, and the exit that closes it.

    Counting rows would charge a single round trip three times and close the
    day after a third of it. In practice the stop and the exit carry no planned
    risk at all; this proves the dedupe holds even if one of them ever did.
    """
    only = await signal("86.08")
    for _ in range(3):
        await submission(signal_id=only.id, planned_risk="86.08")
    assert await allocated() == (Decimal("86.08"), 1)


async def test_a_protective_stop_adds_nothing_to_the_day() -> None:
    """It is placed behind a position that is already paid for out of the budget.

    This is the live shape: the entry declares the risk, and the stop and exit
    written for the same trade declare none.
    """
    only = await signal("86.08")
    await submission(signal_id=only.id, planned_risk="86.08")
    await submission(signal_id=only.id, planned_risk=None)
    await submission(signal_id=only.id, planned_risk=None)
    assert await allocated() == (Decimal("86.08"), 1)


async def test_an_order_that_never_reached_the_broker_allocates_nothing() -> None:
    """PREPARED is written before the request is sent, REJECTED came back no.

    Neither is money at risk, and holding budget against them would stop the
    day on orders that never existed at the broker.
    """
    for status in ("PREPARED", "REJECTED", "FAILED", "UNKNOWN"):
        await submission(signal_id=(await signal("86.08")).id, status=status)
    assert await allocated() == (Decimal("0"), 0)


async def test_a_resolved_placed_order_allocates_its_risk() -> None:
    """An UNKNOWN that recovery proved had reached the broker is a live trade."""
    await submission(signal_id=(await signal("86.08")).id, status="RESOLVED_PLACED")
    assert await allocated() == (Decimal("86.08"), 1)


async def test_yesterdays_live_orders_do_not_spend_todays_budget() -> None:
    await submission(signal_id=(await signal("86.08")).id, when=datetime(2026, 9, 23, 5, 30, tzinfo=UTC))
    assert await allocated() == (Decimal("0"), 0)


async def test_a_paper_signal_with_no_live_order_allocates_nothing() -> None:
    """The whole point of the query: paper is not consulted about live money.

    A paper signal the live path never submitted -- refused by a gate, or
    simply a paper-mode day -- must not hold any of the live account's budget.
    """
    await signal("86.08")
    assert await allocated() == (Decimal("0"), 0)


async def test_the_signals_own_risk_amount_is_not_what_is_counted() -> None:
    """The live row's figure governs, not the paper signal it came from.

    Written as a difference on purpose: a day whose paper record has moved on
    -- a settings save between the two, a signal re-sized -- must still be
    bounded by what the live order itself declared.
    """
    await submission(signal_id=(await signal("500.00")).id, planned_risk="86.08")
    assert await allocated() == (Decimal("86.08"), 1)


async def test_a_row_written_before_the_column_existed_allocates_nothing() -> None:
    """Null is counted as nothing rather than guessed at.

    A default of zero would read as "this order risked nothing", which is the
    same claim by another name, so the column is nullable and a null row simply
    does not take part.
    """
    await submission(signal_id=(await signal("86.08")).id, planned_risk=None)
    assert await allocated() == (Decimal("0"), 0)


async def test_a_live_order_with_no_signal_behind_it_is_its_own_trade() -> None:
    """Nothing links it to another row, so it must not be folded into one.

    Two unlinked orders of the same size are two trades spending twice -- the
    dedupe key falls back to the row's own id precisely so a missing signal
    cannot make them collapse into one.
    """
    await submission(signal_id=None, planned_risk="86.08")
    await submission(signal_id=None, planned_risk="86.08")
    assert await allocated() == (Decimal("172.16"), 2)
