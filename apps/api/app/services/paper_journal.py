"""Look-ahead-safe paper-signal outcome tracking from completed candles only."""

from decimal import Decimal

from sqlalchemy import select

from app.db.models import PaperSignal, PaperSignalOutcome
from app.db.session import SessionLocal
from app.services.market_calculations import CompletedCandle


async def update_outcomes(candle: CompletedCandle) -> None:
    async with SessionLocal() as session:
        signals = list(
            (
                await session.scalars(
                    select(PaperSignal).where(
                        PaperSignal.instrument_token == candle.instrument_token,
                        PaperSignal.session_date == candle.session_date,
                        PaperSignal.candle_opened_at < candle.opened_at,
                    )
                )
            ).all()
        )
        for signal in signals:
            outcome = await session.scalar(
                select(PaperSignalOutcome).where(PaperSignalOutcome.paper_signal_id == signal.id)
            )
            if outcome is None:
                outcome = PaperSignalOutcome(paper_signal_id=signal.id)
                session.add(outcome)
            if outcome.status != "OPEN":
                continue
            unit_risk = abs(signal.entry_price - signal.stop_price)
            if not unit_risk:
                continue
            favorable = candle.high - signal.entry_price if signal.side == "LONG" else signal.entry_price - candle.low
            adverse = candle.low - signal.entry_price if signal.side == "LONG" else signal.entry_price - candle.high
            outcome.mfe_r = max(Decimal(str(outcome.mfe_r)), favorable / unit_risk)
            outcome.mae_r = min(Decimal(str(outcome.mae_r)), adverse / unit_risk)
            target_hit = (
                candle.high >= signal.target_price if signal.side == "LONG" else candle.low <= signal.target_price
            )
            stop_hit = candle.low <= signal.stop_price if signal.side == "LONG" else candle.high >= signal.stop_price
            if target_hit and stop_hit:
                outcome.status, outcome.exit_price, outcome.realized_r = "AMBIGUOUS", None, None
                outcome.resolved_at = candle.closed_at
            elif target_hit or stop_hit:
                exit_price = signal.target_price if target_hit else signal.stop_price
                outcome.status = "TARGET" if target_hit else "STOP"
                outcome.exit_price = exit_price
                outcome.realized_r = (
                    (exit_price - signal.entry_price) / unit_risk
                    if signal.side == "LONG"
                    else (signal.entry_price - exit_price) / unit_risk
                )
                outcome.resolved_at = candle.closed_at
        await session.commit()
