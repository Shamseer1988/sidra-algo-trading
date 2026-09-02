import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import delete, select

from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY, TradingControls
from app.db.models import ApplicationSetting, PaperSignal, RiskReservation
from app.db.session import SessionLocal, engine
from app.services.risk_engine import PaperRiskEngine


async def test_risk_reservation_serializes_concurrent_daily_allocations() -> None:
    await engine.dispose()
    session_date = date(2031, 1, 2)
    async with SessionLocal() as session:
        setting = await session.get(ApplicationSetting, TRADING_KEY)
        controls = TradingControls.model_validate(setting.value if setting else DEFAULT_TRADING_CONTROLS)
    per_signal_risk = (
        Decimal(str(controls.account_capital))
        * Decimal(str(controls.maximum_daily_risk_percent))
        / Decimal("100")
        * Decimal("0.6")
    )
    signals = [
        PaperSignal(
            signal_key=f"risk-reservation-{index}",
            instrument_token=f"NSE:RISK{index}",
            session_date=session_date,
            candle_opened_at=datetime(2031, 1, 2, 4, index, tzinfo=UTC),
            strategy_version="orb-retest-v1@1",
            side="LONG",
            entry_price=Decimal("100"),
            stop_price=Decimal("90"),
            target_price=Decimal("120"),
            quantity=10,
            risk_amount=per_signal_risk,
            score=100,
            score_breakdown={},
            strategy_snapshot={},
            indicator_snapshot={},
        )
        for index in range(2)
    ]
    async with SessionLocal() as session:
        session.add_all(signals)
        await session.commit()
        for signal in signals:
            await session.refresh(signal)
    try:
        decisions = await asyncio.gather(*(PaperRiskEngine().reserve_signal(signal) for signal in signals))
        assert sum(decision.allowed for decision in decisions) == 1
        assert any(decision.reason == "Daily paper-risk allocation limit reached" for decision in decisions)
        async with SessionLocal() as session:
            reservations = list(
                (
                    await session.scalars(select(RiskReservation).where(RiskReservation.session_date == session_date))
                ).all()
            )
        assert {item.status for item in reservations} == {"ACTIVE", "REJECTED"}
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(PaperSignal).where(PaperSignal.session_date == session_date))
            await session.commit()
        await engine.dispose()
