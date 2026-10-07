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


# --- the share-price band, enforced where a trade is decided ------------------
#
# The band existed only in ``universe``, which ranks the watchlist and runs only
# when UNIVERSE_ENABLED is set -- off by default. An operator who set a ₹1,500
# cap on the Settings screen had set nothing: on 7 October the system entered
# PAYTM at ₹1,745 and BHARTIARTL at ₹1,818. The filter also fails open when the
# day's list has not been built, and ranks on previous-day candles, so a share
# that crossed the cap this morning passes it even with everything switched on.


def band(**overrides):
    from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TradingControls

    return TradingControls.model_validate({**DEFAULT_TRADING_CONTROLS, **overrides})


def test_a_share_above_the_cap_is_refused_by_name() -> None:
    from app.services.risk_engine import outside_price_band

    reason = outside_price_band(Decimal("1818.40"), band(universe_max_share_price=1500))
    assert reason is not None
    assert "1818.40" in reason and "1500" in reason
    assert "above" in reason


def test_a_share_below_the_floor_is_refused_too() -> None:
    from app.services.risk_engine import outside_price_band

    reason = outside_price_band(Decimal("35"), band(universe_min_share_price=40))
    assert reason is not None and "below" in reason


def test_a_share_inside_the_band_passes() -> None:
    from app.services.risk_engine import outside_price_band

    assert outside_price_band(Decimal("1200"), band(universe_max_share_price=1500)) is None


def test_the_cap_is_inclusive_of_its_own_value() -> None:
    """₹1,500 is at the cap, not above it. An operator who types a round number
    means that number is allowed."""
    from app.services.risk_engine import outside_price_band

    assert outside_price_band(Decimal("1500"), band(universe_max_share_price=1500)) is None


def test_zero_means_unbounded_so_an_unset_band_changes_nothing() -> None:
    from app.services.risk_engine import outside_price_band

    controls = band(universe_max_share_price=0, universe_min_share_price=0)
    assert outside_price_band(Decimal("48000"), controls) is None
    assert outside_price_band(Decimal("3"), controls) is None


async def test_a_signal_above_the_cap_is_refused_and_the_refusal_is_recorded() -> None:
    """End to end, because the unit above only proves the arithmetic.

    This is the path every signal takes before the live bridge is offered it,
    so refusing here refuses the live order too -- one place, both accounts.
    """
    await engine.dispose()
    session_date = date(2031, 1, 3)
    async with SessionLocal() as session:
        stored = await session.get(ApplicationSetting, TRADING_KEY)
        previous = dict(stored.value) if stored else None
        value = {**(previous or DEFAULT_TRADING_CONTROLS), "universe_max_share_price": 1500.0}
        if stored:
            stored.value = value
        else:
            session.add(ApplicationSetting(key=TRADING_KEY, value=value))
        await session.commit()

    signal = PaperSignal(
        signal_key="risk-band-bhartiartl",
        instrument_token="NSE_EQ|INE397D01024",
        session_date=session_date,
        candle_opened_at=datetime(2031, 1, 3, 4, 5, tzinfo=UTC),
        strategy_version="orb-retest-v1@1",
        side="LONG",
        entry_price=Decimal("1818.40"),
        stop_price=Decimal("1803.85"),
        target_price=Decimal("1847.50"),
        quantity=6,
        risk_amount=Decimal("87.30"),
        score=86,
        score_breakdown={},
        strategy_snapshot={},
        indicator_snapshot={},
    )
    try:
        async with SessionLocal() as session:
            session.add(signal)
            await session.commit()
            await session.refresh(signal)

        decision = await PaperRiskEngine().reserve_signal(signal)

        assert decision.allowed is False
        assert "above" in decision.reason and "1500" in decision.reason
        async with SessionLocal() as session:
            reservation = await session.scalar(
                select(RiskReservation).where(RiskReservation.paper_signal_id == signal.id)
            )
        # Recorded, not silent: the operator reads "we found a setup and refused
        # it" rather than seeing nothing happen at all.
        assert reservation is not None and reservation.status == "REJECTED"
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(RiskReservation).where(RiskReservation.session_date == session_date))
            await session.execute(delete(PaperSignal).where(PaperSignal.session_date == session_date))
            stored = await session.get(ApplicationSetting, TRADING_KEY)
            if stored is not None:
                stored.value = previous if previous is not None else DEFAULT_TRADING_CONTROLS
            await session.commit()
        await engine.dispose()
