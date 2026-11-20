"""The live risk engine's day figures, kept to one desk (step 0.3).

Run against real rows in Postgres: these are the queries the live position
limit and the daily risk budget are computed from, and the engine's own tests
answer them with a fake. A date and tokens nothing else writes to keep these
rows apart.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.db.models import LiveOrderSubmission
from app.db.session import SessionLocal
from app.services.live_risk import live_entries_in_flight, live_risk_allocated_today, other_desk_tokens

DAY = date(2031, 3, 6)
AT = datetime(2031, 3, 6, 4, 30, tzinfo=UTC)
INTRA = "NSE_EQ|INE0RISK0001"
SWING = "NSE_EQ|INE0RISK0002"
BOTH = "NSE_EQ|INE0RISK0003"


async def _clean() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(LiveOrderSubmission).where(LiveOrderSubmission.client_order_id.like("risk-%")))
        await session.commit()


@pytest.fixture(autouse=True)
async def reset():
    await _clean()
    yield
    await _clean()


async def _entry(token: str, desk: str, risk: str = "250") -> None:
    async with SessionLocal() as session:
        session.add(
            LiveOrderSubmission(
                client_order_id=f"risk-{uuid4().hex[:20]}",
                broker="UPSTOX",
                exchange="NSE_EQ",
                trading_symbol=token,
                product="D",
                price_type="MARKET",
                transaction_type="BUY",
                quantity=5,
                status="ACCEPTED",
                planned_risk=Decimal(risk),
                desk=desk,
                created_at=AT,
                request_snapshot={"canonical": {"instrumentToken": token}},
            )
        )
        await session.commit()


async def test_the_intraday_risk_budget_is_not_spent_by_a_swing_entry() -> None:
    await _entry(INTRA, "INTRADAY", "250")
    await _entry(SWING, "SWING", "120")
    async with SessionLocal() as session:
        assert await live_risk_allocated_today(session, DAY) == (Decimal("250"), 1)
        assert await live_risk_allocated_today(session, DAY, desk="SWING") == (Decimal("120"), 1)
        assert await live_risk_allocated_today(session, DAY, desk=None) == (Decimal("370"), 2)


async def test_a_swing_entry_in_flight_does_not_hold_an_intraday_slot() -> None:
    await _entry(INTRA, "INTRADAY")
    await _entry(SWING, "SWING")
    moment = AT + timedelta(seconds=10)
    async with SessionLocal() as session:
        assert await live_entries_in_flight(session, DAY, now=moment) == (INTRA,)
        assert set(await live_entries_in_flight(session, DAY, now=moment, desk=None)) == {INTRA, SWING}


async def test_a_share_only_the_swing_desk_entered_is_the_swing_desks() -> None:
    await _entry(INTRA, "INTRADAY")
    await _entry(SWING, "SWING")
    async with SessionLocal() as session:
        assert await other_desk_tokens(session, DAY) == {SWING}


async def test_a_share_both_desks_entered_stays_in_the_intraday_count() -> None:
    """The broker holds it as one position; counting it is the side that refuses."""
    await _entry(BOTH, "INTRADAY")
    await _entry(BOTH, "SWING")
    async with SessionLocal() as session:
        assert await other_desk_tokens(session, DAY) == set()
