"""Settings > Charges: the two rate cards, and a worked example to check them by.

Every save is one recorded change (a settings revision and an audit entry,
with the reason), like any other setting. Nothing here reaches a broker, and
no live risk gate reads these figures; see ``app.services.charges``.
"""

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, DbSession, require_roles
from app.db.models import ApplicationSetting, AuditLog, User, UserRole
from app.services.charges import (
    SWING_CHARGES_KEY,
    SwingChargeSettings,
    intraday_charges,
    intraday_round_trip,
    swing_charges,
    swing_round_trip,
)
from app.services.paper_execution import PAPER_EXECUTION_KEY, PaperExecutionControls
from app.services.settings_history import record_revision, summarise

router = APIRouter(prefix="/charges", tags=["Charges"])

# The example trade: about one intraday position at today's capital and risk.
EXAMPLE_VALUE = Decimal("50000")


class RoundTripView(BaseModel):
    trade_value: float
    buy: float
    sell: float
    total: float
    lines: dict[str, float]


class ChargesView(BaseModel):
    intraday: PaperExecutionControls
    swing: SwingChargeSettings
    intraday_example: RoundTripView
    swing_example: RoundTripView


class IntradayChargesRequest(BaseModel):
    charges: PaperExecutionControls
    reason: str | None = Field(default=None, max_length=300)


class SwingChargesRequest(BaseModel):
    charges: SwingChargeSettings
    reason: str | None = Field(default=None, max_length=300)


class PreviewRequest(BaseModel):
    intraday: PaperExecutionControls | None = None
    swing: SwingChargeSettings | None = None
    trade_value: float = Field(default=float(EXAMPLE_VALUE), gt=0, le=10_000_000)


class PreviewView(BaseModel):
    intraday: RoundTripView | None
    swing: RoundTripView | None


def _round_trip(trip) -> RoundTripView:  # noqa: ANN001
    data = trip.as_dict()
    return RoundTripView(
        trade_value=float(data["trade_value"]),
        buy=float(data["buy"]),
        sell=float(data["sell"]),
        total=float(data["total"]),
        lines={key: float(value) for key, value in data["lines"].items()},
    )


async def _view(session) -> ChargesView:  # noqa: ANN001
    intraday = await intraday_charges(session)
    swing = await swing_charges(session)
    return ChargesView(
        intraday=intraday,
        swing=swing,
        intraday_example=_round_trip(intraday_round_trip(intraday, EXAMPLE_VALUE)),
        swing_example=_round_trip(swing_round_trip(swing, EXAMPLE_VALUE)),
    )


async def save_card(
    session,  # noqa: ANN001
    key: str,
    before: dict[str, Any],
    after: dict[str, Any],
    user: User,
    event_type: str,
    reason: str | None,
) -> None:
    """Store one rate card as one recorded change. The caller commits."""
    row = await session.get(ApplicationSetting, key)
    if row is None:
        session.add(ApplicationSetting(key=key, value=after, updated_by_user_id=user.id))
    else:
        row.value, row.updated_by_user_id = after, user.id
    summary = summarise(before, after)
    await record_revision(session, key, after, summary, changed_by_user_id=user.id)
    session.add(
        AuditLog(
            user_id=user.id,
            event_type=event_type,
            metadata_json={"changed": list(summary.changed_keys), "reason": reason, "paper_only": True},
        )
    )


@router.get("", response_model=ChargesView)
async def charges(_: CurrentUser, session: DbSession) -> ChargesView:
    return await _view(session)


@router.put("/intraday", response_model=ChargesView)
async def update_intraday(
    request: IntradayChargesRequest,
    session: DbSession,
    user: User = Depends(require_roles(UserRole.ADMIN)),
) -> ChargesView:
    before = (await intraday_charges(session)).model_dump()
    await save_card(
        session,
        PAPER_EXECUTION_KEY,
        before,
        request.charges.model_dump(),
        user,
        "settings.intraday_charges_updated",
        request.reason,
    )
    await session.commit()
    return await _view(session)


@router.put("/swing", response_model=ChargesView)
async def update_swing(
    request: SwingChargesRequest,
    session: DbSession,
    user: User = Depends(require_roles(UserRole.ADMIN)),
) -> ChargesView:
    before = (await swing_charges(session)).model_dump()
    await save_card(
        session,
        SWING_CHARGES_KEY,
        before,
        request.charges.model_dump(),
        user,
        "settings.swing_charges_updated",
        request.reason,
    )
    await session.commit()
    return await _view(session)


@router.post("/preview", response_model=PreviewView)
async def preview(request: PreviewRequest, _: CurrentUser) -> PreviewView:
    """What a round trip would cost under figures not yet saved. Writes nothing."""
    value = Decimal(str(request.trade_value))
    return PreviewView(
        intraday=_round_trip(intraday_round_trip(request.intraday, value)) if request.intraday else None,
        swing=_round_trip(swing_round_trip(request.swing, value)) if request.swing else None,
    )
