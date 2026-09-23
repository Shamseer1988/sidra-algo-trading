"""Daily profit target and loss limit.

These are the two controls an operator reaches for after a bad morning, so the
behaviour that matters is that they stop the day when the money says so — not
when the risk budget says so, which is a different control that was already
there and does not do this job.

The loss limit counts open positions. A session sitting on a large unrealised
loss has lost the money whether or not the trade has closed, and a limit that
waits for the booking is a limit that lets the next trade through at exactly the
wrong moment.
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY, TradingControls
from app.db.models import ApplicationSetting, PaperPosition, PaperSignal, RiskReservation
from app.db.session import SessionLocal
from app.services.risk_engine import PaperRiskEngine

SESSION = datetime(2026, 9, 24, tzinfo=UTC).date()
TOKEN = "NSE_EQ|INEPNLTEST"


async def controls(**overrides) -> None:
    profile = {**DEFAULT_TRADING_CONTROLS, "account_capital": 10000.0, "risk_per_trade_percent": 1.0, **overrides}
    TradingControls.model_validate(profile)
    async with SessionLocal() as session:
        setting = await session.get(ApplicationSetting, TRADING_KEY)
        if setting is None:
            session.add(ApplicationSetting(key=TRADING_KEY, value=profile))
        else:
            setting.value = profile
        await session.commit()


async def clean() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(RiskReservation).where(RiskReservation.instrument_token == TOKEN))
        await session.execute(delete(PaperPosition).where(PaperPosition.instrument_token == TOKEN))
        await session.execute(delete(PaperSignal).where(PaperSignal.instrument_token == TOKEN))
        await session.execute(delete(ApplicationSetting).where(ApplicationSetting.key == TRADING_KEY))
        await session.commit()


async def make_signal() -> PaperSignal:
    async with SessionLocal() as session:
        signal = PaperSignal(
            signal_key=f"pnl-{uuid4()}",
            instrument_token=TOKEN,
            session_date=SESSION,
            candle_opened_at=datetime(2026, 9, 24, tzinfo=UTC),
            strategy_version="ema-momentum-v1@1",
            side="LONG",
            entry_price=Decimal("650"),
            stop_price=Decimal("647.55"),
            target_price=Decimal("653.675"),
            quantity=40,
            risk_amount=Decimal("100"),
            score=75,
            score_breakdown={},
            strategy_snapshot={},
            indicator_snapshot={},
        )
        session.add(signal)
        await session.commit()
        await session.refresh(signal)
        return signal


async def book(realized: str, unrealized: str = "0", fees: str = "0", status: str = "CLOSED") -> None:
    """Record a position whose total_pnl is realized + unrealized - fees."""
    signal = await make_signal()
    async with SessionLocal() as session:
        total = Decimal(realized) + Decimal(unrealized) - Decimal(fees)
        session.add(
            PaperPosition(
                paper_signal_id=signal.id,
                instrument_token=TOKEN,
                session_date=SESSION,
                strategy_version="ema-momentum-v1@1",
                side="LONG",
                status=status,
                initial_quantity=40,
                open_quantity=40 if status != "CLOSED" else 0,
                average_entry_price=Decimal("650"),
                stop_price=Decimal("647.55"),
                target_price=Decimal("653.675"),
                realized_pnl=Decimal(realized),
                unrealized_pnl=Decimal(unrealized),
                fees_total=Decimal(fees),
                total_pnl=total,
            )
        )
        await session.commit()


@pytest.fixture(autouse=True)
async def reset():
    await clean()
    yield
    await clean()


async def decide() -> str:
    return (await PaperRiskEngine().reserve_signal(await make_signal())).reason


# --- the loss limit -------------------------------------------------------


async def test_a_day_inside_its_loss_limit_keeps_trading() -> None:
    await controls(daily_loss_limit=1000.0, maximum_daily_risk_percent=10.0)
    await book(realized="-400")
    assert await decide() == "Paper risk reserved"


async def test_the_loss_limit_stops_the_day() -> None:
    await controls(daily_loss_limit=1000.0, maximum_daily_risk_percent=10.0)
    await book(realized="-1000")
    assert await decide() == "Daily loss limit reached"


async def test_an_open_losing_position_counts_towards_the_limit() -> None:
    """The money is gone whether or not the trade has closed."""
    await controls(daily_loss_limit=1000.0, maximum_daily_risk_percent=10.0)
    await book(realized="-600", unrealized="-500", status="OPEN")
    assert await decide() == "Daily loss limit reached"


async def test_costs_count_towards_the_limit() -> None:
    """A day is down by what it paid as well as by what it lost."""
    await controls(daily_loss_limit=1000.0, maximum_daily_risk_percent=10.0)
    await book(realized="-900", fees="150")
    assert await decide() == "Daily loss limit reached"


# --- the profit target ----------------------------------------------------


async def test_the_profit_target_stops_the_day() -> None:
    await controls(daily_profit_target=2000.0, maximum_daily_risk_percent=10.0)
    await book(realized="2000")
    assert await decide() == "Daily profit target reached"


async def test_a_day_below_its_profit_target_keeps_trading() -> None:
    await controls(daily_profit_target=2000.0, maximum_daily_risk_percent=10.0)
    await book(realized="1500")
    assert await decide() == "Paper risk reserved"


async def test_fees_are_subtracted_before_the_target_is_judged() -> None:
    """2,050 gross less 100 of costs has not made 2,000."""
    await controls(daily_profit_target=2000.0, maximum_daily_risk_percent=10.0)
    await book(realized="2050", fees="100")
    assert await decide() == "Paper risk reserved"


# --- interaction and defaults --------------------------------------------


async def test_zero_disables_each_limit() -> None:
    """The default configuration must behave exactly as it did before."""
    await controls(daily_loss_limit=0.0, daily_profit_target=0.0, maximum_daily_risk_percent=10.0)
    await book(realized="-50000")
    assert await decide() == "Paper risk reserved"


async def test_the_loss_limit_is_checked_before_the_profit_target() -> None:
    """A day that is down cannot also be a day that is finished winning."""
    await controls(daily_loss_limit=1000.0, daily_profit_target=2000.0, maximum_daily_risk_percent=10.0)
    await book(realized="-1200")
    assert await decide() == "Daily loss limit reached"


async def test_the_limits_are_separate_from_the_risk_budget() -> None:
    """maximum_daily_risk_percent caps risk allocated, not money lost."""
    await controls(daily_loss_limit=1000.0, maximum_daily_risk_percent=1.0)
    await book(realized="0")
    # 1% of 10,000 is 100, and one signal risks exactly 100, so the budget binds
    # first even though the day has lost nothing.
    assert await decide() == "Paper risk reserved"
    assert await decide() == "Daily paper-risk allocation limit reached"


async def test_yesterdays_losses_do_not_stop_today() -> None:
    await controls(daily_loss_limit=1000.0, maximum_daily_risk_percent=10.0)
    signal = await make_signal()
    async with SessionLocal() as session:
        session.add(
            PaperPosition(
                paper_signal_id=signal.id,
                instrument_token=TOKEN,
                session_date=datetime(2026, 9, 23, tzinfo=UTC).date(),
                strategy_version="ema-momentum-v1@1",
                side="LONG",
                status="CLOSED",
                initial_quantity=40,
                open_quantity=0,
                average_entry_price=Decimal("650"),
                stop_price=Decimal("647.55"),
                target_price=Decimal("653.675"),
                realized_pnl=Decimal("-5000"),
                total_pnl=Decimal("-5000"),
            )
        )
        await session.commit()
    assert await decide() == "Paper risk reserved"
