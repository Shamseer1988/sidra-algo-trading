"""Transactional controls for paper-risk allocation; intentionally broker-independent."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select, text

from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY, TradingControls
from app.db.models import ApplicationSetting, PaperPosition, PaperSignal, RiskReservation
from app.db.session import SessionLocal
from app.services import daily_limits

# Lives in its own module so the live path can enforce the same band without
# importing this engine. Re-exported here because every existing caller asks
# the paper engine for it.
from app.services.price_band import outside_price_band


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str
    reservation_id: str | None = None


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def strategy_type_of(strategy_version: str | None) -> str:
    """``"rs-pullback-v1@8"`` -> ``"rs-pullback-v1"``; the type is what is stable."""
    return (strategy_version or "").split("@", 1)[0]


def same_book(candidate_type: str, other_type: str, paper_only_types: tuple[str, ...]) -> bool:
    """Whether another signal's risk shares this candidate's paper budget.

    The same split the trade ceilings use (``trade_counter``): a paper-only
    strategy (``live_orders=false``) is measured as if it traded the account
    alone, so it counts only its own reservations; every other strategy counts
    every strategy except the paper-only ones.

    Without this, the paper journal was one budget for all of them. On
    19 November RS Pullback's three paper mirrors spent the day's 750 at
    9:46 AM, and CPR's only signal -- SUNPHARMA at 11:25 AM -- was refused with
    "Daily paper-risk allocation limit reached". A paper-only test that can only
    trade on days the live strategy is quiet is measuring the quiet days.

    Nothing live reads this. The live account's allocation, position and
    exposure limits are enforced from live submissions in ``live_risk``, and a
    paper-only strategy never reaches the broker at all.
    """
    if candidate_type in paper_only_types:
        return other_type == candidate_type
    return other_type not in paper_only_types


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
            # Imported here: the registry pulls in every strategy module, and
            # this engine is imported by the scanner that also imports them.
            from app.services.strategy_registry import StrategyRegistry

            paper_only_types = await StrategyRegistry.paper_only_types(session)
            candidate_type = strategy_type_of(signal.strategy_version)
            reservations = [
                reservation
                for reservation, version in (
                    await session.execute(
                        select(RiskReservation, PaperSignal.strategy_version)
                        .join(PaperSignal, PaperSignal.id == RiskReservation.paper_signal_id)
                        .where(RiskReservation.session_date == signal.session_date)
                        .with_for_update(of=RiskReservation)
                    )
                ).all()
                if same_book(candidate_type, strategy_type_of(version), paper_only_types)
            ]
            positions = [
                position
                for position in (
                    await session.scalars(
                        select(PaperPosition)
                        .where(
                            PaperPosition.session_date == signal.session_date,
                            PaperPosition.status.in_(["OPENING", "OPEN", "REDUCING"]),
                        )
                        .with_for_update()
                    )
                ).all()
                if same_book(candidate_type, strategy_type_of(position.strategy_version), paper_only_types)
            ]
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
            leverage_mult = (
                _decimal(controls.intraday_leverage_multiplier)
                if getattr(controls, "intraday_leverage_enabled", False)
                else Decimal("1.0")
            )
            exposure_limit = (
                _decimal(controls.account_capital)
                * _decimal(controls.maximum_open_exposure_percent)
                * leverage_mult
                / 100
            )
            # The day's standing: a halt already recorded, or a fresh look at the
            # money. Delegated so that this and paper execution cannot disagree
            # about what "the limit was reached" means, and latched so that a
            # day which has finished cannot un-finish when an open winner gives
            # back its gains.
            # The paper day's P&L, from the paper ledger. Live reads the broker
            # instead; the two sources never meet, which is the point.
            session_pnl = sum(
                (
                    _decimal(item.total_pnl)
                    for item in (
                        await session.scalars(
                            select(PaperPosition).where(PaperPosition.session_date == signal.session_date)
                        )
                    ).all()
                ),
                start=Decimal("0"),
            )
            verdict = await daily_limits.verdict_for(
                session,
                signal.session_date,
                daily_limits.PAPER,
                session_pnl=session_pnl,
                controls=controls,
            )

            reason = "Paper risk reserved"
            if verdict.halted:
                reason = verdict.reason or reason
                # Written here as well as in paper execution because a limit can
                # first be crossed by a signal arriving rather than by a price
                # moving, and whichever notices first owns recording it.
                await daily_limits.record_halt(session, signal.session_date, daily_limits.PAPER, verdict)
            elif (banded := outside_price_band(signal.entry_price, controls)) is not None:
                # Before the allocation checks: this is a fact about the share,
                # not about the day, and "we are not trading this stock" is a
                # clearer thing to read than "the budget is full".
                reason = banded
            elif reserved + risk_amount > daily_limit:
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
