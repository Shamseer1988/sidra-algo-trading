from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from app.db.models import OmsOrder, OmsOrderEvent, OrderIntent, PaperSignal, ShadowOrder
from app.db.session import SessionLocal, engine
from app.services.oms import OmsTransitionError, PaperOmsGateway, transition_order


async def test_paper_oms_intent_is_idempotent_and_lifecycle_is_explicit() -> None:
    signal = PaperSignal(
        signal_key="test-oms-idempotency",
        instrument_token="NSE:OMS",
        session_date=datetime(2026, 9, 2, tzinfo=UTC).date(),
        candle_opened_at=datetime(2026, 9, 2, tzinfo=UTC),
        strategy_version="orb-retest-v1@1",
        side="LONG",
        entry_price=Decimal("100"),
        stop_price=Decimal("98"),
        target_price=Decimal("104"),
        quantity=10,
        risk_amount=Decimal("20"),
        score=100,
        score_breakdown={},
        strategy_snapshot={},
        indicator_snapshot={},
    )
    try:
        async with SessionLocal() as session:
            session.add(signal)
            await session.commit()
            await session.refresh(signal)
            gateway = PaperOmsGateway()
            first = await gateway.ensure_entry(session, signal)
            second = await gateway.ensure_entry(session, signal)
            assert first.id == second.id
            shadow = await session.scalar(select(ShadowOrder).where(ShadowOrder.oms_order_id == first.id))
            assert shadow is not None and shadow.intended_price == Decimal("100")
            await transition_order(session, first, "UNKNOWN", "transport_timeout")
            assert first.unknown_since is not None
            await transition_order(session, first, "ACKNOWLEDGED", "reconciled")
            await transition_order(session, first, "FILLED", "paper_fill_recorded")
            with pytest.raises(OmsTransitionError):
                await transition_order(session, first, "UNKNOWN", "late_timeout")
            await session.commit()
        async with SessionLocal() as session:
            stored = await session.scalar(
                select(OmsOrder).join(OrderIntent).where(OrderIntent.source_paper_signal_id == signal.id)
            )
            assert stored is not None and stored.status == "FILLED"
            events = list(
                (await session.scalars(select(OmsOrderEvent).where(OmsOrderEvent.oms_order_id == stored.id))).all()
            )
            assert [event.to_status for event in events] == ["QUEUED", "UNKNOWN", "ACKNOWLEDGED", "FILLED"]
    finally:
        async with SessionLocal() as session:
            intents = list(
                (
                    await session.scalars(select(OrderIntent).where(OrderIntent.source_paper_signal_id == signal.id))
                ).all()
            )
            for intent in intents:
                order = await session.scalar(select(OmsOrder).where(OmsOrder.order_intent_id == intent.id))
                if order:
                    await session.execute(delete(OmsOrderEvent).where(OmsOrderEvent.oms_order_id == order.id))
                    await session.delete(order)
                await session.delete(intent)
            await session.execute(delete(PaperSignal).where(PaperSignal.signal_key == signal.signal_key))
            await session.commit()
        await engine.dispose()
