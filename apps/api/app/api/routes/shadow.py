"""Zero-submission shadow-mode comparison APIs."""

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession
from app.db.models import ShadowOrder

router = APIRouter(prefix="/shadow", tags=["Shadow mode"])


class ShadowOrderResponse(BaseModel):
    id: str
    instrument_token: str
    side: str
    intended_quantity: int
    intended_price: float
    comparison_status: str
    paper_fill_price: float | None
    price_delta: float | None
    observed_at: datetime
    compared_at: datetime | None


class ShadowSummary(BaseModel):
    intended_orders: int
    compared_orders: int
    awaiting_paper_fill: int
    average_price_delta: float
    broker_submissions: int = 0


@router.get("/orders", response_model=list[ShadowOrderResponse])
async def orders(_: CurrentUser, session: DbSession) -> list[ShadowOrderResponse]:
    rows = list((await session.scalars(select(ShadowOrder).order_by(ShadowOrder.created_at.desc()).limit(250))).all())
    return [_row(item) for item in rows]


@router.get("/summary", response_model=ShadowSummary)
async def summary(_: CurrentUser, session: DbSession) -> ShadowSummary:
    total = int(await session.scalar(select(func.count(ShadowOrder.id))) or 0)
    compared = int(
        await session.scalar(
            select(func.count(ShadowOrder.id)).where(
                ShadowOrder.comparison_status.in_(["COMPARED", "PARTIALLY_COMPARED"])
            )
        )
        or 0
    )
    awaiting = int(
        await session.scalar(
            select(func.count(ShadowOrder.id)).where(ShadowOrder.comparison_status == "AWAITING_PAPER_FILL")
        )
        or 0
    )
    average = await session.scalar(
        select(func.avg(ShadowOrder.price_delta)).where(ShadowOrder.price_delta.is_not(None))
    )
    return ShadowSummary(
        intended_orders=total,
        compared_orders=compared,
        awaiting_paper_fill=awaiting,
        average_price_delta=float(average or 0),
    )


def _row(item: ShadowOrder) -> ShadowOrderResponse:
    return ShadowOrderResponse(
        id=str(item.id),
        instrument_token=item.instrument_token,
        side=item.side,
        intended_quantity=item.intended_quantity,
        intended_price=float(item.intended_price),
        comparison_status=item.comparison_status,
        paper_fill_price=float(item.paper_fill_price) if item.paper_fill_price is not None else None,
        price_delta=float(item.price_delta) if item.price_delta is not None else None,
        observed_at=item.observed_at,
        compared_at=item.compared_at,
    )
