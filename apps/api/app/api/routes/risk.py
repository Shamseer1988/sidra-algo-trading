"""Verified paper-risk exposure and reservation reporting."""

from datetime import date
from decimal import Decimal

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY, TradingControls
from app.db.models import ApplicationSetting, PaperPosition, RiskReservation

router = APIRouter(prefix="/risk", tags=["Paper risk"])


class PaperRiskSummary(BaseModel):
    session_date: date
    daily_risk_limit: float
    daily_risk_allocated: float
    daily_risk_available: float
    maximum_open_positions: int
    active_reservations: int
    open_positions: int
    exposure_limit: float
    current_exposure: float
    exposure_available: float
    # Surfaced so the screen can say "₹50,000 = 100% of ₹10,000 at 5x" rather
    # than a bare number the operator has to reverse-engineer.
    leverage_multiplier: float
    rejected_reservations: int


@router.get("/summary", response_model=PaperRiskSummary)
async def summary(_: CurrentUser, session: DbSession, session_date: date | None = None) -> PaperRiskSummary:
    target_date = session_date or date.today()
    setting = await session.get(ApplicationSetting, TRADING_KEY)
    controls = TradingControls.model_validate(setting.value if setting else DEFAULT_TRADING_CONTROLS)
    reservations = list(
        (await session.scalars(select(RiskReservation).where(RiskReservation.session_date == target_date))).all()
    )
    positions = list(
        (
            await session.scalars(
                select(PaperPosition).where(
                    PaperPosition.session_date == target_date,
                    PaperPosition.status.in_(["OPENING", "OPEN", "REDUCING"]),
                )
            )
        ).all()
    )
    daily_limit = Decimal(str(controls.account_capital)) * Decimal(str(controls.maximum_daily_risk_percent)) / 100
    # Leverage belongs in this product, exactly as it does in risk_engine.py.
    # It was missing here, so the screen reported a fifth of the ceiling the
    # engine actually enforces -- an understatement, which reads as reassurance
    # and is the worse direction to be wrong in.
    leverage = (
        Decimal(str(controls.intraday_leverage_multiplier)) if controls.intraday_leverage_enabled else Decimal("1")
    )
    exposure_limit = (
        Decimal(str(controls.account_capital)) * Decimal(str(controls.maximum_open_exposure_percent)) * leverage / 100
    )
    allocated = sum(
        (Decimal(str(item.risk_amount)) for item in reservations if item.status in {"ACTIVE", "SETTLED"}),
        start=Decimal("0"),
    )
    exposure = sum(
        (Decimal(str(item.average_entry_price or 0)) * item.open_quantity for item in positions), start=Decimal("0")
    )
    return PaperRiskSummary(
        session_date=target_date,
        daily_risk_limit=float(daily_limit),
        daily_risk_allocated=float(allocated),
        daily_risk_available=float(max(Decimal("0"), daily_limit - allocated)),
        maximum_open_positions=controls.maximum_open_positions,
        active_reservations=sum(item.status == "ACTIVE" for item in reservations),
        open_positions=len(positions),
        exposure_limit=float(exposure_limit),
        current_exposure=float(exposure),
        exposure_available=float(max(Decimal("0"), exposure_limit - exposure)),
        leverage_multiplier=float(leverage),
        rejected_reservations=sum(item.status == "REJECTED" for item in reservations),
    )
