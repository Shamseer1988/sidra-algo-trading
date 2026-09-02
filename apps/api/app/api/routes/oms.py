"""OMS observability endpoints. They expose paper state only and cannot submit orders."""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, require_roles
from app.db.models import AuditLog, ExecutionReconciliation, OmsOrder, OmsOrderEvent, OrderIntent, User, UserRole
from app.services.oms import reconcile_paper_oms

router = APIRouter(prefix="/oms", tags=["OMS"])


class OmsOrderResponse(BaseModel):
    id: str
    intent_id: str
    idempotency_key: str
    instrument_token: str
    side: str
    quantity: int
    mode: str
    venue: str
    status: str
    filled_quantity: int
    average_fill_price: float | None
    unknown_since: datetime | None
    created_at: datetime
    updated_at: datetime


class OmsEventResponse(BaseModel):
    sequence: int
    from_status: str | None
    to_status: str
    event_type: str
    occurred_at: datetime


class ReconciliationResponse(BaseModel):
    id: str
    mode: str
    status: str
    internal_orders: int
    external_orders: int
    unknown_orders: int
    detail: str
    created_at: datetime


def _order(order: OmsOrder, intent: OrderIntent) -> OmsOrderResponse:
    return OmsOrderResponse(
        id=str(order.id),
        intent_id=str(intent.id),
        idempotency_key=intent.idempotency_key,
        instrument_token=intent.instrument_token,
        side=intent.side,
        quantity=intent.quantity,
        mode=intent.mode,
        venue=order.venue,
        status=order.status,
        filled_quantity=order.filled_quantity,
        average_fill_price=float(order.average_fill_price) if order.average_fill_price is not None else None,
        unknown_since=order.unknown_since,
        created_at=order.created_at,
        updated_at=order.updated_at,
    )


@router.get("/orders", response_model=list[OmsOrderResponse])
async def orders(
    _: CurrentUser, session: DbSession, limit: int = Query(default=100, ge=1, le=250)
) -> list[OmsOrderResponse]:
    rows = list(
        (
            await session.execute(
                select(OmsOrder, OrderIntent)
                .join(OrderIntent, OmsOrder.order_intent_id == OrderIntent.id)
                .order_by(OmsOrder.updated_at.desc())
                .limit(limit)
            )
        ).all()
    )
    return [_order(order, intent) for order, intent in rows]


@router.get("/orders/{order_id}/events", response_model=list[OmsEventResponse])
async def order_events(order_id: str, _: CurrentUser, session: DbSession) -> list[OmsEventResponse]:
    rows = list(
        (
            await session.scalars(
                select(OmsOrderEvent).where(OmsOrderEvent.oms_order_id == order_id).order_by(OmsOrderEvent.sequence)
            )
        ).all()
    )
    return [
        OmsEventResponse(
            sequence=row.sequence,
            from_status=row.from_status,
            to_status=row.to_status,
            event_type=row.event_type,
            occurred_at=row.occurred_at,
        )
        for row in rows
    ]


@router.get("/reconciliations", response_model=list[ReconciliationResponse])
async def reconciliations(_: CurrentUser, session: DbSession) -> list[ReconciliationResponse]:
    rows = list(
        (
            await session.scalars(
                select(ExecutionReconciliation).order_by(ExecutionReconciliation.created_at.desc()).limit(50)
            )
        ).all()
    )
    return [_reconciliation(row) for row in rows]


@router.post("/reconciliations/run", response_model=ReconciliationResponse)
async def run_reconciliation(
    session: DbSession, user: User = Depends(require_roles(UserRole.ADMIN))
) -> ReconciliationResponse:
    result = await reconcile_paper_oms(session)
    session.add(
        AuditLog(
            user_id=user.id,
            event_type="oms.paper_reconciled",
            metadata_json={"status": result.status, "paper_only": True},
        )
    )
    await session.commit()
    await session.refresh(result)
    return _reconciliation(result)


def _reconciliation(row: ExecutionReconciliation) -> ReconciliationResponse:
    return ReconciliationResponse(
        id=str(row.id),
        mode=row.mode,
        status=row.status,
        internal_orders=row.internal_orders,
        external_orders=row.external_orders,
        unknown_orders=row.unknown_orders,
        detail=row.detail,
        created_at=row.created_at,
    )
