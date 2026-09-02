"""Transactional controls for paper-risk allocation; intentionally broker-independent."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select, text

from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY, TradingControls
from app.db.models import ApplicationSetting, PaperPosition, PaperSignal, RiskReservation
from app.db.session import SessionLocal


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str
    reservation_id: str | None = None


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


class PaperRiskEngine:
    """Serializes paper signal allocations before a simulated entry order is queued."""

    async def reserve_signal(self, signal: PaperSignal) -> RiskDecision:
        async with SessionLocal() as session:
            # This application is PostgreSQL-only. The per-session advisory lock closes
            # the empty-ledger race that row locking alone cannot protect.
            await session.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": 6112026})
            existing = await session.scalar(
                select(RiskReservation).where(RiskReservation.paper_signal_id == signal.id).with_for_update()
            )
            if existing:
                return RiskDecision(
                    allowed=existing.status in {"ACTIVE", "SETTLED"},
                    reason=existing.decision_reason,
                    reservation_id=str(existing.id),
                )
            setting = await session.get(ApplicationSetting, TRADING_KEY)
            controls = TradingControls.model_validate(setting.value if setting else DEFAULT_TRADING_CONTROLS)
            reservations = list(
                (
                    await session.scalars(
                        select(RiskReservation)
                        .where(RiskReservation.session_date == signal.session_date)
                        .with_for_update()
                    )
                ).all()
            )
            positions = list(
                (
                    await session.scalars(
                        select(PaperPosition)
                        .where(
                            PaperPosition.session_date == signal.session_date,
                            PaperPosition.status.in_(["OPENING", "OPEN", "REDUCING"]),
                        )
                        .with_for_update()
                    )
                ).all()
            )
            risk_amount = _decimal(signal.risk_amount)
            daily_limit = _decimal(controls.account_capital) * _decimal(controls.maximum_daily_risk_percent) / 100
            reserved = sum(
                (_decimal(item.risk_amount) for item in reservations if item.status in {"ACTIVE", "SETTLED"}),
                start=Decimal("0"),
            )
            active_reservations = sum(item.status == "ACTIVE" for item in reservations)
            current_exposure = sum(
                (_decimal(item.average_entry_price or 0) * item.open_quantity for item in positions),
                start=Decimal("0"),
            )
            candidate_exposure = _decimal(signal.entry_price) * signal.quantity
            exposure_limit = _decimal(controls.account_capital) * _decimal(controls.maximum_open_exposure_percent) / 100
            reason = "Paper risk reserved"
            if reserved + risk_amount > daily_limit:
                reason = "Daily paper-risk allocation limit reached"
            elif active_reservations >= controls.maximum_open_positions:
                reason = "Maximum concurrent paper positions reached"
            elif current_exposure + candidate_exposure > exposure_limit:
                reason = "Maximum paper exposure limit reached"
            accepted = reason == "Paper risk reserved"
            reservation = RiskReservation(
                paper_signal_id=signal.id,
                session_date=signal.session_date,
                instrument_token=signal.instrument_token,
                risk_amount=risk_amount,
                status="ACTIVE" if accepted else "REJECTED",
                decision_reason=reason,
            )
            session.add(reservation)
            await session.commit()
            return RiskDecision(allowed=accepted, reason=reason, reservation_id=str(reservation.id))

    async def settle_signal(self, paper_signal_id) -> None:
        async with SessionLocal() as session:
            reservation = await session.scalar(
                select(RiskReservation).where(RiskReservation.paper_signal_id == paper_signal_id).with_for_update()
            )
            if reservation and reservation.status == "ACTIVE":
                reservation.status = "SETTLED"
                reservation.released_at = datetime.now(UTC)
                reservation.decision_reason = "Paper position closed; daily allocation retained"
                await session.commit()
