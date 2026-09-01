"""Paper-execution ledger APIs. These endpoints cannot call a broker."""

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession, require_roles
from app.db.models import ApplicationSetting, AuditLog, PaperFill, PaperOrder, PaperPosition, User, UserRole
from app.services.paper_execution import (
    DEFAULT_PAPER_EXECUTION_CONTROLS,
    PAPER_EXECUTION_KEY,
    PaperExecutionControls,
)

router = APIRouter(prefix="/paper", tags=["Paper execution"])


class PaperOrderResponse(BaseModel):
    id: str
    paper_signal_id: str
    client_order_id: str
    instrument_token: str
    session_date: str
    side: str
    order_type: str
    order_role: str
    status: str
    quantity: int
    filled_quantity: int
    average_fill_price: float | None
    limit_price: float | None
    stop_price: float | None
    fee_total: float
    eligible_after: datetime
    rejection_reason: str | None
    created_at: datetime


class PaperFillResponse(BaseModel):
    id: str
    paper_order_id: str
    instrument_token: str
    side: str
    quantity: int
    price: float
    gross_value: float
    slippage_amount: float
    total_fees: float
    occurred_at: datetime


class PaperPositionResponse(BaseModel):
    id: str
    paper_signal_id: str
    instrument_token: str
    session_date: str
    strategy_version: str
    side: str
    status: str
    initial_quantity: int
    open_quantity: int
    average_entry_price: float | None
    average_exit_price: float | None
    current_price: float | None
    stop_price: float
    target_price: float
    realized_pnl: float
    unrealized_pnl: float
    fees_total: float
    total_pnl: float
    opened_at: datetime | None
    closed_at: datetime | None


class PaperExecutionSummary(BaseModel):
    orders: int
    pending_orders: int
    fills: int
    open_positions: int
    realized_pnl: float
    unrealized_pnl: float
    total_pnl: float
    fees_total: float


def _order(row: PaperOrder) -> PaperOrderResponse:
    return PaperOrderResponse(
        id=str(row.id),
        paper_signal_id=str(row.paper_signal_id),
        client_order_id=row.client_order_id,
        instrument_token=row.instrument_token,
        session_date=row.session_date.isoformat(),
        side=row.side,
        order_type=row.order_type,
        order_role=row.order_role,
        status=row.status,
        quantity=row.quantity,
        filled_quantity=row.filled_quantity,
        average_fill_price=float(row.average_fill_price) if row.average_fill_price is not None else None,
        limit_price=float(row.limit_price) if row.limit_price is not None else None,
        stop_price=float(row.stop_price) if row.stop_price is not None else None,
        fee_total=float(row.fee_total),
        eligible_after=row.eligible_after,
        rejection_reason=row.rejection_reason,
        created_at=row.created_at,
    )


def _position(row: PaperPosition) -> PaperPositionResponse:
    return PaperPositionResponse(
        id=str(row.id),
        paper_signal_id=str(row.paper_signal_id),
        instrument_token=row.instrument_token,
        session_date=row.session_date.isoformat(),
        strategy_version=row.strategy_version,
        side=row.side,
        status=row.status,
        initial_quantity=row.initial_quantity,
        open_quantity=row.open_quantity,
        average_entry_price=float(row.average_entry_price) if row.average_entry_price is not None else None,
        average_exit_price=float(row.average_exit_price) if row.average_exit_price is not None else None,
        current_price=float(row.current_price) if row.current_price is not None else None,
        stop_price=float(row.stop_price),
        target_price=float(row.target_price),
        realized_pnl=float(row.realized_pnl),
        unrealized_pnl=float(row.unrealized_pnl),
        fees_total=float(row.fees_total),
        total_pnl=float(row.total_pnl),
        opened_at=row.opened_at,
        closed_at=row.closed_at,
    )


@router.get("/orders", response_model=list[PaperOrderResponse])
async def orders(_: CurrentUser, session: DbSession) -> list[PaperOrderResponse]:
    rows = list((await session.scalars(select(PaperOrder).order_by(PaperOrder.created_at.desc()).limit(250))).all())
    return [_order(row) for row in rows]


@router.get("/fills", response_model=list[PaperFillResponse])
async def fills(_: CurrentUser, session: DbSession) -> list[PaperFillResponse]:
    rows = list((await session.scalars(select(PaperFill).order_by(PaperFill.occurred_at.desc()).limit(500))).all())
    return [
        PaperFillResponse(
            id=str(row.id),
            paper_order_id=str(row.paper_order_id),
            instrument_token=row.instrument_token,
            side=row.side,
            quantity=row.quantity,
            price=float(row.price),
            gross_value=float(row.gross_value),
            slippage_amount=float(row.slippage_amount),
            total_fees=float(row.total_fees),
            occurred_at=row.occurred_at,
        )
        for row in rows
    ]


@router.get("/positions", response_model=list[PaperPositionResponse])
async def positions(_: CurrentUser, session: DbSession) -> list[PaperPositionResponse]:
    rows = list(
        (await session.scalars(select(PaperPosition).order_by(PaperPosition.updated_at.desc()).limit(250))).all()
    )
    return [_position(row) for row in rows]


@router.get("/summary", response_model=PaperExecutionSummary)
async def summary(_: CurrentUser, session: DbSession) -> PaperExecutionSummary:
    orders_count = await session.scalar(select(func.count(PaperOrder.id)))
    pending = await session.scalar(
        select(func.count(PaperOrder.id)).where(PaperOrder.status.in_(["PENDING", "PARTIALLY_FILLED"]))
    )
    fills_count = await session.scalar(select(func.count(PaperFill.id)))
    positions_rows = list((await session.scalars(select(PaperPosition))).all())
    return PaperExecutionSummary(
        orders=int(orders_count or 0),
        pending_orders=int(pending or 0),
        fills=int(fills_count or 0),
        open_positions=sum(row.status != "CLOSED" for row in positions_rows),
        realized_pnl=float(sum((row.realized_pnl for row in positions_rows), start=0)),
        unrealized_pnl=float(sum((row.unrealized_pnl for row in positions_rows), start=0)),
        total_pnl=float(sum((row.total_pnl for row in positions_rows), start=0)),
        fees_total=float(sum((row.fees_total for row in positions_rows), start=0)),
    )


@router.get("/controls", response_model=PaperExecutionControls)
async def controls(_: CurrentUser, session: DbSession) -> PaperExecutionControls:
    setting = await session.get(ApplicationSetting, PAPER_EXECUTION_KEY)
    return PaperExecutionControls.model_validate(setting.value if setting else DEFAULT_PAPER_EXECUTION_CONTROLS)


@router.put("/controls", response_model=PaperExecutionControls)
async def update_controls(
    controls: PaperExecutionControls,
    session: DbSession,
    user: User = Depends(require_roles(UserRole.ADMIN)),
) -> PaperExecutionControls:
    setting = await session.get(ApplicationSetting, PAPER_EXECUTION_KEY)
    if setting is None:
        session.add(
            ApplicationSetting(key=PAPER_EXECUTION_KEY, value=controls.model_dump(), updated_by_user_id=user.id)
        )
    else:
        setting.value, setting.updated_by_user_id = controls.model_dump(), user.id
    session.add(
        AuditLog(user_id=user.id, event_type="paper.execution_controls_updated", metadata_json={"paper_only": True})
    )
    await session.commit()
    return controls
