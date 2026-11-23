"""Paper-execution ledger APIs. These endpoints cannot call a broker."""

from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from app.api.deps import CurrentUser, DbSession, require_roles
from app.db.models import ApplicationSetting, PaperFill, PaperOrder, PaperPosition, PaperSignal, User, UserRole
from app.services.market_calculations import MARKET_TIMEZONE
from app.services.paper_execution import (
    DEFAULT_PAPER_EXECUTION_CONTROLS,
    PAPER_EXECUTION_KEY,
    PaperExecutionControls,
)
from app.services.trade_history import SCRATCH, strategy_family, traded_live_ids
from app.services.trading_symbols import resolve_script_names

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


class PaperDayOrder(PaperOrderResponse):
    script_name: str
    strategy: str
    live_copy: bool


class PaperDayPosition(PaperPositionResponse):
    script_name: str
    strategy: str
    live_copy: bool


class PaperDayTotals(BaseModel):
    trades: int
    open_positions: int
    wins: int
    losses: int
    scratches: int
    gross_pnl: float
    charges: float
    net_pnl: float


class PaperDayStrategy(PaperDayTotals):
    strategy: str


class PaperDayResponse(BaseModel):
    session_date: str
    totals: PaperDayTotals
    pending_orders: int
    live_copies: int
    strategies: list[PaperDayStrategy]
    positions: list[PaperDayPosition]
    orders: list[PaperDayOrder]


def _day_totals(rows: list[PaperPosition]) -> dict:
    closed = [row for row in rows if row.status == "CLOSED"]
    wins = sum(row.total_pnl > SCRATCH for row in closed)
    losses = sum(row.total_pnl < -SCRATCH for row in closed)
    return {
        "trades": len(rows),
        "open_positions": len(rows) - len(closed),
        "wins": wins,
        "losses": losses,
        "scratches": len(closed) - wins - losses,
        "gross_pnl": float(sum(row.realized_pnl + row.unrealized_pnl for row in rows)),
        "charges": float(sum(row.fees_total for row in rows)),
        "net_pnl": float(sum(row.total_pnl for row in rows)),
    }


@router.get("/day", response_model=PaperDayResponse)
async def day(
    _: CurrentUser,
    session: DbSession,
    strategy: Annotated[str | None, Query(description="One strategy family, any revision.")] = None,
    session_date: Annotated[date | None, Query(description="Defaults to today, IST.")] = None,
) -> PaperDayResponse:
    """One session's simulated orders and positions, totalled by the server.

    Today's view also carries any position still open, or order still pending,
    from an earlier session: hiding one because of its date would read as flat.

    The simulated copy of a trade that reached the broker is listed and marked,
    and left out of every total, so these totals are the ones History and the
    P&L calendar give for paper. The real trade is under Live.
    """
    today = datetime.now(MARKET_TIMEZONE).date()
    wanted = session_date or today
    in_day = PaperPosition.session_date == wanted
    order_in_day = PaperOrder.session_date == wanted
    if wanted == today:
        in_day = or_(in_day, PaperPosition.status != "CLOSED")
        order_in_day = or_(order_in_day, PaperOrder.status.in_(["PENDING", "PARTIALLY_FILLED"]))
    rows = list(
        (await session.scalars(select(PaperPosition).where(in_day).order_by(PaperPosition.opened_at.desc()))).all()
    )
    order_rows = (
        await session.execute(
            select(PaperOrder, PaperSignal.strategy_version)
            .join(PaperSignal, PaperSignal.id == PaperOrder.paper_signal_id)
            .where(order_in_day)
            .order_by(PaperOrder.created_at.desc())
        )
    ).all()
    names = await resolve_script_names(
        session, {row.instrument_token for row in rows} | {order.instrument_token for order, _version in order_rows}
    )
    dates = [row.session_date for row in rows] + [order.session_date for order, _version in order_rows]
    live = await traded_live_ids(session, min(dates), max(dates)) if dates else set()
    families: dict[str, list[PaperPosition]] = {}
    for _order_row, version in order_rows:
        families.setdefault(strategy_family(version), [])
    for row in rows:
        own = families.setdefault(strategy_family(row.strategy_version), [])
        if row.paper_signal_id not in live:
            own.append(row)
    if strategy:
        rows = [row for row in rows if strategy_family(row.strategy_version) == strategy]
        order_rows = [pair for pair in order_rows if strategy_family(pair[1]) == strategy]
    return PaperDayResponse(
        session_date=wanted.isoformat(),
        totals=PaperDayTotals(**_day_totals([row for row in rows if row.paper_signal_id not in live])),
        pending_orders=sum(
            order.status in ("PENDING", "PARTIALLY_FILLED") and order.paper_signal_id not in live
            for order, _version in order_rows
        ),
        live_copies=sum(row.paper_signal_id in live for row in rows),
        strategies=[PaperDayStrategy(strategy=name, **_day_totals(own)) for name, own in sorted(families.items())],
        positions=[
            PaperDayPosition(
                **_position(row).model_dump(),
                script_name=names[row.instrument_token],
                strategy=strategy_family(row.strategy_version),
                live_copy=row.paper_signal_id in live,
            )
            for row in rows
        ],
        orders=[
            PaperDayOrder(
                **_order(order).model_dump(),
                script_name=names[order.instrument_token],
                strategy=strategy_family(version),
                live_copy=order.paper_signal_id in live,
            )
            for order, version in order_rows
        ],
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
    # The same card as Settings > Charges, saved the same way: one recorded change.
    from app.api.routes.charges import save_card

    setting = await session.get(ApplicationSetting, PAPER_EXECUTION_KEY)
    before = PaperExecutionControls.model_validate(
        setting.value if setting else DEFAULT_PAPER_EXECUTION_CONTROLS
    ).model_dump()
    await save_card(
        session, PAPER_EXECUTION_KEY, before, controls.model_dump(), user, "paper.execution_controls_updated", None
    )
    await session.commit()
    return controls
