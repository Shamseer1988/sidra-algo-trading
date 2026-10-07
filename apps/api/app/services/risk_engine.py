"""Transactional controls for paper-risk allocation; intentionally broker-independent."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select, text

from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY, TradingControls
from app.db.models import ApplicationSetting, PaperPosition, PaperSignal, RiskReservation
from app.db.session import SessionLocal
from app.services import daily_limits


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str
    reservation_id: str | None = None


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def outside_price_band(entry_price: object, controls: TradingControls) -> str | None:
    """Why this share is outside the operator's price band, or None.

    The band existed only in ``universe``, which ranks the watchlist, and that
    module runs only when ``UNIVERSE_ENABLED`` is set -- off by default. So an
    operator who set a ₹1,500 cap on the Settings screen had set nothing: on
    7 October the system entered PAYTM at ₹1,745 and BHARTIARTL at ₹1,818. The
    universe filter also fails open when the day's list has not been built yet,
    and it ranks on previous-day candles, so a share that crossed the cap this
    morning would pass it even with everything switched on.

    So the band is enforced here instead, where a trade is actually decided: on
    the signal's own entry price, in the engine every signal must pass, whether
    or not the dynamic universe is running and whatever it decided. The refusal
    is recorded against the signal, so the operator reads "we found a setup and
    refused it" rather than seeing nothing at all.

    Only the saved control is used, and zero means unbounded. The environment
    values behind ``universe`` are deployment defaults for ranking a watchlist;
    a refusal to trade should come from the number the operator typed.

    The reason this matters is not neatness. A price cap is how a risk budget is
    kept spendable in whole shares: ₹100 of risk against a ₹1,818 share with a
    0.8% stop buys six, and against a ₹4,700 one buys one -- a position that
    spends the budget and earns a fraction of the target.
    """
    price = _decimal(entry_price)
    high = _decimal(getattr(controls, "universe_max_share_price", 0) or 0)
    low = _decimal(getattr(controls, "universe_min_share_price", 0) or 0)
    if high > 0 and price > high:
        return f"Share price {price} is above the {high} cap"
    if low > 0 and price < low:
        return f"Share price {price} is below the {low} floor"
    return None


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
