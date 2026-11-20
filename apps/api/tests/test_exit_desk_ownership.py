"""The intraday exits manage intraday positions and nothing else (step 0.2, change 3).

``_signal_for`` decides which trade a broker position belongs to, and so whose
target and square-off it is managed against. These run it against real rows in
Postgres -- the module's own tests replace it with a fake, and the lesson of
this path is that a fake answers whatever it is asked.

A session date nothing else writes to keeps these rows apart from other tests.
"""

from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.db.models import LiveOrderSubmission, PaperSignal
from app.db.session import SessionLocal
from app.services import live_exit_manager as module
from app.services.broker_adapter import BrokerPositionRecord
from app.services.trade_counter import session_bounds_utc

DAY = date(2031, 3, 4)
START, END = session_bounds_utc(DAY)
TOKEN = "NSE_EQ|INE0DESK0001"
POSITION = BrokerPositionRecord(symbol="DESKTEST", net_quantity=Decimal("5"), instrument_token=TOKEN)


async def _entry(session, desk: str, minutes: int, *, order_type: str = "MARKET") -> PaperSignal:  # noqa: ANN001
    signal = PaperSignal(
        signal_key=f"desk-own-{uuid4()}",
        instrument_token=TOKEN,
        session_date=DAY,
        candle_opened_at=START + timedelta(hours=4),
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
    await session.flush()
    session.add(
        LiveOrderSubmission(
            client_order_id=f"desk-own-{uuid4().hex[:20]}",
            paper_signal_id=signal.id,
            broker="UPSTOX",
            exchange="NSE_EQ",
            trading_symbol=TOKEN,
            product="D",
            price_type="SL-M" if order_type == "SL-M" else "MARKET",
            transaction_type="BUY",
            quantity=5,
            status="ACCEPTED",
            desk=desk,
            created_at=START + timedelta(hours=4, minutes=minutes),
            request_snapshot={"canonical": {"instrumentToken": TOKEN, "orderType": order_type, "product": "DELIVERY"}},
        )
    )
    await session.flush()
    return signal


@pytest.fixture
async def session():
    async with SessionLocal() as db:
        yield db
        await db.rollback()
    async with SessionLocal() as db:
        await db.execute(delete(LiveOrderSubmission).where(LiveOrderSubmission.client_order_id.like("desk-own-%")))
        await db.execute(delete(PaperSignal).where(PaperSignal.instrument_token == TOKEN))
        await db.commit()


async def test_an_intraday_position_is_still_matched_to_its_latest_entry(session) -> None:
    await _entry(session, "INTRADAY", 1)
    latest = await _entry(session, "INTRADAY", 30)
    found = await module._signal_for(session, POSITION, START, END)
    assert found.id == latest.id


async def test_a_swing_position_is_not_the_intraday_exits_to_manage(session) -> None:
    await _entry(session, "SWING", 5)
    with pytest.raises(module.HeldByAnotherDesk) as held:
        await module._signal_for(session, POSITION, START, END)
    assert held.value.conflict is False


async def test_both_desks_on_one_share_is_a_conflict_not_an_intraday_trade(session) -> None:
    """The broker nets them into one position; closing it would sell swing shares."""
    await _entry(session, "SWING", 5)
    await _entry(session, "INTRADAY", 30)
    with pytest.raises(module.HeldByAnotherDesk) as held:
        await module._signal_for(session, POSITION, START, END)
    assert held.value.conflict is True


async def test_a_swing_stop_alone_does_not_claim_the_position(session) -> None:
    """Stops are skipped as before; only entries say whose trade it is."""
    await _entry(session, "SWING", 5, order_type="SL-M")
    intraday = await _entry(session, "INTRADAY", 30)
    found = await module._signal_for(session, POSITION, START, END)
    assert found.id == intraday.id


# --- what the sweep says -------------------------------------------------------


async def _consider_with(monkeypatch: pytest.MonkeyPatch, desks: set[str]):
    async def held(*_args):
        raise module.HeldByAnotherDesk(desks)

    monkeypatch.setattr(module, "_signal_for", held)
    return await module._consider(None, None, POSITION, Decimal("5"), module.datetime.now(module.UTC), START, END)


async def test_a_swing_holding_is_reported_quietly_and_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    result = await _consider_with(monkeypatch, {"SWING"})
    assert result.step == "other_desk"
    assert result.acted is False
    outcome = module.ExitSweepOutcome(ran=True, step="swept", detail="", exits=[result])
    assert outcome.noteworthy == []


async def test_a_desk_conflict_is_left_alone_and_reported_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    result = await _consider_with(monkeypatch, {"SWING", "INTRADAY"})
    assert result.step == "desk_conflict"
    assert result.acted is False
    outcome = module.ExitSweepOutcome(ran=True, step="swept", detail="", exits=[result])
    assert [item.step for item in outcome.noteworthy] == ["desk_conflict"]
