"""The daily ceilings count the intraday desk's trades only (step 0.2, change 4).

Every ceiling -- account trades, per share, per sector, per strategy -- is read
through ``count_filled_entries`` or ``filled_entry_instruments``, which now
default to the intraday desk. The swing desk is bounded by open positions and
open risk; a swing entry spending one of the intraday desk's three trades would
stop the intraday desk for a reason that has nothing to do with it.

A date and tokens nothing else writes to keep these rows apart from other tests.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.db.models import LiveOrderSubmission, PaperOrder, PaperSignal
from app.db.session import SessionLocal
from app.services.trade_counter import count_filled_entries, filled_entry_instruments, session_bounds_utc

DAY = date(2031, 3, 5)
INTRA = "NSE_EQ|INE0CEIL0001"
SWING = "NSE_EQ|INE0CEIL0002"


async def _clean() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(PaperOrder).where(PaperOrder.instrument_token.in_((INTRA, SWING))))
        await session.execute(delete(PaperSignal).where(PaperSignal.instrument_token.in_((INTRA, SWING))))
        start, end = session_bounds_utc(DAY)
        await session.execute(
            delete(LiveOrderSubmission).where(
                LiveOrderSubmission.created_at >= start, LiveOrderSubmission.created_at < end
            )
        )
        await session.commit()


@pytest.fixture(autouse=True)
async def reset():
    await _clean()
    yield
    await _clean()


async def _signal(token: str, desk: str, session_date: date = DAY) -> PaperSignal:
    async with SessionLocal() as session:
        signal = PaperSignal(
            signal_key=f"ceil-{uuid4()}",
            instrument_token=token,
            session_date=session_date,
            candle_opened_at=datetime.combine(session_date, datetime.min.time(), tzinfo=UTC) + timedelta(hours=4),
            strategy_version="rs-pullback-v1@1",
            side="LONG",
            entry_price=Decimal("100"),
            stop_price=Decimal("98"),
            target_price=Decimal("104"),
            quantity=5,
            risk_amount=Decimal("10"),
            score=80,
            desk=desk,
        )
        session.add(signal)
        await session.commit()
        await session.refresh(signal)
        return signal


async def _paper_fill(signal: PaperSignal) -> None:
    """A filled paper entry today, whatever day its signal was raised."""
    async with SessionLocal() as session:
        session.add(
            PaperOrder(
                paper_signal_id=signal.id,
                client_order_id=f"ceil-{uuid4()}",
                instrument_token=signal.instrument_token,
                session_date=DAY,
                strategy_version=signal.strategy_version,
                side="BUY",
                order_type="MARKET",
                order_role="ENTRY",
                quantity=5,
                filled_quantity=5,
                status="FILLED",
                eligible_after=datetime(2031, 3, 5, 4, 0, tzinfo=UTC),
            )
        )
        await session.commit()


async def _live_entry(token: str, desk: str, signal: PaperSignal | None = None) -> None:
    async with SessionLocal() as session:
        session.add(
            LiveOrderSubmission(
                client_order_id=f"ceil-{uuid4().hex[:20]}",
                paper_signal_id=signal.id if signal else None,
                broker="UPSTOX",
                exchange="NSE_EQ",
                trading_symbol=token,
                product="D",
                price_type="MARKET",
                transaction_type="BUY",
                quantity=5,
                status="ACCEPTED",
                planned_risk=Decimal("10"),
                desk=desk,
                created_at=datetime(2031, 3, 5, 4, 30, tzinfo=UTC),
                request_snapshot={"canonical": {"instrumentToken": token}},
            )
        )
        await session.commit()


async def _total(**kwargs) -> int:
    async with SessionLocal() as session:
        return (await count_filled_entries(session, DAY, **kwargs)).total


async def test_a_swing_paper_fill_does_not_spend_an_intraday_trade() -> None:
    await _paper_fill(await _signal(INTRA, "INTRADAY"))
    await _paper_fill(await _signal(SWING, "SWING"))
    assert await _total() == 1
    assert await _total(desk="SWING") == 1
    assert await _total(desk=None) == 2


async def test_a_swing_fill_from_yesterdays_signal_is_still_the_swing_desks() -> None:
    """The end-of-day scan raises the signal; the entry fills the next morning."""
    yesterday = await _signal(SWING, "SWING", session_date=DAY - timedelta(days=1))
    await _paper_fill(yesterday)
    assert await _total() == 0
    assert await _total(desk="SWING") == 1


async def test_a_swing_live_entry_does_not_spend_an_intraday_trade() -> None:
    await _live_entry(INTRA, "INTRADAY", await _signal(INTRA, "INTRADAY"))
    await _live_entry(SWING, "SWING", await _signal(SWING, "SWING"))
    assert await _total() == 1
    assert await _total(desk="SWING") == 1


async def test_a_live_order_with_no_signal_still_counts_for_the_intraday_desk() -> None:
    """Every such order so far was intraday, and an order at the exchange must be counted somewhere."""
    await _live_entry(INTRA, "INTRADAY")
    assert await _total() == 1


async def test_the_per_share_ceiling_ignores_a_swing_trade_on_that_share() -> None:
    await _paper_fill(await _signal(INTRA, "SWING"))
    assert await _total(instrument_token=INTRA) == 0


async def test_the_sector_ceiling_sees_only_intraday_shares() -> None:
    await _paper_fill(await _signal(INTRA, "INTRADAY"))
    await _paper_fill(await _signal(SWING, "SWING"))
    await _live_entry(SWING, "SWING", await _signal(SWING, "SWING"))
    async with SessionLocal() as session:
        assert await filled_entry_instruments(session, DAY) == (INTRA,)
        assert set(await filled_entry_instruments(session, DAY, desk=None)) == {INTRA, SWING}
