"""What happens to a position that is open when the day's limit is reached.

Blocking new entries is not enough on its own, and the gap it leaves is not
hypothetical: a day stopped at -1,050 whose open trade then ran to -1,800 has
honoured the letter of a 1,000 limit and none of its intent. So reaching a limit
also exits what is running.

These tests drive real candles through the paper order manager rather than
writing positions by hand, because the behaviour under test is a sequence — mark
to market, notice, cancel, exit on the next candle — and each step only means
anything in the presence of the one before it.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY, TradingControls
from app.db.models import (
    ApplicationSetting,
    PaperOrder,
    PaperPosition,
    PaperSessionHalt,
    PaperSignal,
    RiskReservation,
)
from app.db.session import SessionLocal
from app.services.market_calculations import CompletedCandle
from app.services.paper_execution import PaperOrderManager

TOKEN = "NSE_EQ|INEHALTTEST"
START = datetime(2026, 9, 25, 4, 0, tzinfo=UTC)


def candle(minute: int, *, close: str, open_price: str | None = None) -> CompletedCandle:
    opened_at = START + timedelta(minutes=minute)
    price = open_price or close
    high = max(Decimal(price), Decimal(close))
    low = min(Decimal(price), Decimal(close))
    return CompletedCandle(
        instrument_token=TOKEN,
        timeframe_seconds=60,
        opened_at=opened_at,
        closed_at=opened_at + timedelta(minutes=1),
        open=Decimal(price),
        high=high,
        low=low,
        close=Decimal(close),
        volume=10_000,
        tick_count=10,
    )


SESSION = candle(0, close="100").session_date


async def set_controls(**overrides) -> None:
    profile = {
        **DEFAULT_TRADING_CONTROLS,
        "account_capital": 10000.0,
        "maximum_daily_risk_percent": 10.0,
        **overrides,
    }
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
        await session.execute(delete(PaperSessionHalt).where(PaperSessionHalt.session_date == SESSION))
        await session.execute(delete(RiskReservation).where(RiskReservation.instrument_token == TOKEN))
        await session.execute(delete(PaperPosition).where(PaperPosition.instrument_token == TOKEN))
        await session.execute(delete(PaperOrder).where(PaperOrder.instrument_token == TOKEN))
        await session.execute(delete(PaperSignal).where(PaperSignal.instrument_token == TOKEN))
        await session.execute(delete(ApplicationSetting).where(ApplicationSetting.key == TRADING_KEY))
        await session.commit()


@pytest.fixture(autouse=True)
async def reset():
    await clean()
    yield
    await clean()


async def open_a_position(manager: PaperOrderManager) -> PaperSignal:
    """Fill an entry, with the bracket exits deliberately out of reach.

    The stop and target are pushed far away so that nothing but the daily limit
    can close this position. A test where two mechanisms could produce the same
    ending does not say which one did.
    """
    signal = PaperSignal(
        signal_key="halt-test",
        instrument_token=TOKEN,
        session_date=SESSION,
        candle_opened_at=START,
        strategy_version="orb-retest-v1@1",
        side="LONG",
        entry_price=Decimal("100"),
        stop_price=Decimal("1"),
        target_price=Decimal("10000"),
        quantity=10,
        risk_amount=Decimal("20"),
        score=100,
        score_breakdown={},
        strategy_snapshot={},
        indicator_snapshot={},
    )
    async with SessionLocal() as session:
        session.add(signal)
        await session.commit()
        await session.refresh(signal)
    await manager.queue_signal(signal)
    await manager.process_completed_candle(candle(1, close="100"))
    return signal


async def roles() -> dict[str, str]:
    async with SessionLocal() as session:
        orders = list((await session.scalars(select(PaperOrder).where(PaperOrder.instrument_token == TOKEN))).all())
    return {order.order_role: order.status for order in orders}


async def position() -> PaperPosition | None:
    async with SessionLocal() as session:
        return await session.scalar(select(PaperPosition).where(PaperPosition.instrument_token == TOKEN))


async def halt() -> PaperSessionHalt | None:
    async with SessionLocal() as session:
        return await session.scalar(select(PaperSessionHalt).where(PaperSessionHalt.session_date == SESSION))


# --- the loss limit -------------------------------------------------------


async def test_a_position_open_when_the_loss_limit_is_reached_is_exited() -> None:
    """The gap this closes: -1,050 blocked, then ran on to -1,800."""
    await set_controls(daily_loss_limit=50.0)
    manager = PaperOrderManager()
    await open_a_position(manager)
    assert (await position()).status == "OPEN"

    # 10 shares bought near 100, now marked at 90: about -100 against a 50 limit.
    await manager.process_completed_candle(candle(2, close="90"))

    record = await halt()
    assert record is not None and record.reason == "Daily loss limit reached"
    after_halt = await roles()
    assert after_halt["TARGET"] == "CANCELLED"
    assert after_halt["STOP"] == "CANCELLED"
    assert after_halt["HALT"] == "PENDING"
    # Still open: the exit is an order, not a stroke of the pen.
    assert (await position()).status == "OPEN"

    await manager.process_completed_candle(candle(3, close="90", open_price="90"))
    assert (await roles())["HALT"] == "FILLED"
    assert (await position()).status == "CLOSED"


async def test_the_exit_fills_at_the_next_candle_rather_than_the_current_mark() -> None:
    """Booking it at the mark would make the journal flatter than the truth.

    You cannot leave a position at the price you decided to leave it, and the
    journal is the evidence this system is judged on.
    """
    await set_controls(daily_loss_limit=50.0)
    manager = PaperOrderManager()
    await open_a_position(manager)
    await manager.process_completed_candle(candle(2, close="90"))
    # The market keeps falling between the decision and the exit.
    await manager.process_completed_candle(candle(3, close="85", open_price="85"))

    async with SessionLocal() as session:
        exit_order = await session.scalar(
            select(PaperOrder).where(PaperOrder.instrument_token == TOKEN, PaperOrder.order_role == "HALT")
        )
    assert exit_order.status == "FILLED"
    # Filled from the next candle's open (85), not the mark that triggered it (90).
    assert exit_order.average_fill_price < Decimal("90")


# --- the profit target ----------------------------------------------------


async def test_a_position_open_when_the_target_is_reached_is_exited_too() -> None:
    """Symmetric on purpose.

    A day declared finished at +2,200 that drifts to +800 with the position
    still open has not finished; it has only stopped looking.
    """
    await set_controls(daily_profit_target=50.0)
    manager = PaperOrderManager()
    await open_a_position(manager)
    await manager.process_completed_candle(candle(2, close="110"))

    record = await halt()
    assert record is not None and record.reason == "Daily profit target reached"
    assert (await roles())["HALT"] == "PENDING"


# --- restraint ------------------------------------------------------------


async def test_a_day_inside_both_limits_leaves_the_position_alone() -> None:
    await set_controls(daily_loss_limit=1000.0, daily_profit_target=2000.0)
    manager = PaperOrderManager()
    await open_a_position(manager)
    await manager.process_completed_candle(candle(2, close="99"))

    assert await halt() is None
    after = await roles()
    assert "HALT" not in after
    assert after["TARGET"] == "PENDING"
    assert (await position()).status == "OPEN"


async def test_the_day_is_flattened_once_not_on_every_later_candle() -> None:
    """A second exit would book against a quantity that is no longer there."""
    await set_controls(daily_loss_limit=50.0)
    manager = PaperOrderManager()
    await open_a_position(manager)
    for minute, close in ((2, "90"), (3, "89"), (4, "88"), (5, "87")):
        await manager.process_completed_candle(candle(minute, close=close, open_price=close))

    async with SessionLocal() as session:
        halts = list(
            (await session.scalars(select(PaperSessionHalt).where(PaperSessionHalt.session_date == SESSION))).all()
        )
        exits = list(
            (
                await session.scalars(
                    select(PaperOrder).where(PaperOrder.instrument_token == TOKEN, PaperOrder.order_role == "HALT")
                )
            ).all()
        )
    assert len(halts) == 1
    assert len(exits) == 1
    assert (await position()).open_quantity == 0
