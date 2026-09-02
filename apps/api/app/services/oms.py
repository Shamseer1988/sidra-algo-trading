"""Paper-only OMS state machine. No class in this module calls a broker SDK."""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ExecutionReconciliation,
    OmsOrder,
    OmsOrderEvent,
    OrderIntent,
    PaperOrder,
    PaperSignal,
    ShadowOrder,
)

TERMINAL_STATES = {"FILLED", "CANCELLED", "REJECTED"}
ALLOWED_TRANSITIONS = {
    "QUEUED": {"ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED", "CANCELLED", "REJECTED", "UNKNOWN"},
    "ACKNOWLEDGED": {"PARTIALLY_FILLED", "FILLED", "CANCELLED", "REJECTED", "UNKNOWN"},
    "PARTIALLY_FILLED": {"PARTIALLY_FILLED", "FILLED", "CANCELLED", "UNKNOWN"},
    "UNKNOWN": {"ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED", "CANCELLED", "REJECTED"},
    "FILLED": set(),
    "CANCELLED": set(),
    "REJECTED": set(),
}


class OmsTransitionError(ValueError):
    pass


async def _append_event(
    session: AsyncSession, order: OmsOrder, from_status: str | None, to_status: str, event_type: str, payload: dict
) -> None:
    sequence = int(
        await session.scalar(
            select(func.coalesce(func.max(OmsOrderEvent.sequence), 0)).where(OmsOrderEvent.oms_order_id == order.id)
        )
        or 0
    )
    session.add(
        OmsOrderEvent(
            oms_order_id=order.id,
            sequence=sequence + 1,
            from_status=from_status,
            to_status=to_status,
            event_type=event_type,
            payload=payload,
        )
    )


async def transition_order(
    session: AsyncSession,
    order: OmsOrder,
    to_status: str,
    event_type: str,
    payload: dict | None = None,
) -> None:
    target = to_status.upper()
    if target == order.status:
        return
    if target not in ALLOWED_TRANSITIONS.get(order.status, set()):
        raise OmsTransitionError(f"Cannot transition OMS order from {order.status} to {target}")
    previous = order.status
    order.status = target
    order.last_transition_at = datetime.now(UTC)
    order.unknown_since = datetime.now(UTC) if target == "UNKNOWN" else None
    await _append_event(session, order, previous, target, event_type, payload or {})


class PaperOmsGateway:
    """Creates idempotent OMS records for an existing paper signal only."""

    async def ensure_entry(self, session: AsyncSession, signal: PaperSignal) -> OmsOrder:
        idempotency_key = f"paper-signal:{signal.id}:entry"
        existing = await session.scalar(select(OrderIntent).where(OrderIntent.idempotency_key == idempotency_key))
        if existing:
            order = await session.scalar(select(OmsOrder).where(OmsOrder.order_intent_id == existing.id))
            if order is None:
                raise OmsTransitionError("Existing paper intent has no OMS order")
            return order
        intent = OrderIntent(
            idempotency_key=idempotency_key,
            source_paper_signal_id=signal.id,
            mode="PAPER",
            instrument_token=signal.instrument_token,
            side="BUY" if signal.side == "LONG" else "SELL",
            quantity=signal.quantity,
            order_type="MARKET",
            intent_role="ENTRY",
            payload_snapshot={
                "paper_only": True,
                "strategy_version": signal.strategy_version,
                "signal_key": signal.signal_key,
            },
        )
        session.add(intent)
        await session.flush()
        order = OmsOrder(order_intent_id=intent.id, venue="PAPER_SIMULATOR", status="QUEUED")
        session.add(order)
        await session.flush()
        session.add(
            ShadowOrder(
                oms_order_id=order.id,
                instrument_token=signal.instrument_token,
                side=intent.side,
                intended_quantity=signal.quantity,
                intended_price=signal.entry_price,
            )
        )
        await _append_event(session, order, None, "QUEUED", "intent_accepted", {"paper_only": True})
        return order

    async def record_fill(self, session: AsyncSession, paper_order: PaperOrder) -> None:
        if paper_order.oms_order_id is None:
            return
        order = await session.get(OmsOrder, paper_order.oms_order_id, with_for_update=True)
        if order is None or order.status in TERMINAL_STATES:
            return
        order.filled_quantity = paper_order.filled_quantity
        order.average_fill_price = paper_order.average_fill_price
        target = "FILLED" if paper_order.status == "FILLED" else "PARTIALLY_FILLED"
        await transition_order(
            session,
            order,
            target,
            "paper_fill_recorded",
            {"filled_quantity": paper_order.filled_quantity, "paper_order_id": str(paper_order.id)},
        )
        shadow = await session.scalar(select(ShadowOrder).where(ShadowOrder.oms_order_id == order.id).with_for_update())
        if shadow and paper_order.average_fill_price is not None:
            shadow.paper_fill_price = paper_order.average_fill_price
            shadow.price_delta = paper_order.average_fill_price - shadow.intended_price
            shadow.comparison_status = "COMPARED" if paper_order.status == "FILLED" else "PARTIALLY_COMPARED"
            shadow.compared_at = datetime.now(UTC)


async def reconcile_paper_oms(session: AsyncSession) -> ExecutionReconciliation:
    internal_orders = int(await session.scalar(select(func.count(OmsOrder.id))) or 0)
    unknown_orders = int(await session.scalar(select(func.count(OmsOrder.id)).where(OmsOrder.status == "UNKNOWN")) or 0)
    orphaned = int(
        await session.scalar(
            select(func.count(OmsOrder.id))
            .outerjoin(PaperOrder, PaperOrder.oms_order_id == OmsOrder.id)
            .where(PaperOrder.id.is_(None))
        )
        or 0
    )
    status = "REQUIRES_REVIEW" if unknown_orders or orphaned else "CLEAN"
    detail = (
        "Paper OMS has no external broker side; internal links are consistent."
        if status == "CLEAN"
        else f"Paper OMS requires review: {unknown_orders} unknown and {orphaned} unlinked orders."
    )
    reconciliation = ExecutionReconciliation(
        mode="PAPER",
        status=status,
        internal_orders=internal_orders,
        external_orders=0,
        unknown_orders=unknown_orders,
        detail=detail,
    )
    session.add(reconciliation)
    return reconciliation
