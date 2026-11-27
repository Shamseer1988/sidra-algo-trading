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
from sqlalchemy import delete, select

from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY, TradingControls
from app.db.models import (
    ApplicationSetting,
    PaperPosition,
    PaperSignal,
    RiskReservation,
    SessionHalt,
)
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
        # The halt is keyed on the session date alone, and it latches — leaving
        # one behind would silently stop every later test's day.
        await session.execute(delete(SessionHalt).where(SessionHalt.session_date == SESSION))
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


# --- the latch ------------------------------------------------------------
#
# The behaviour these protect is the one that was wrong before: the limits were
# recomputed on every check, so a day that had finished could un-finish. That is
# a filter, not a stop, and the difference is visible only in the case below.


async def test_a_reached_target_stays_reached_when_an_open_winner_gives_it_back() -> None:
    """The case that made this a latch.

    A position showing +2,200 halts the day. It then closes at +800. Without the
    latch the next signal recomputes 800, finds it under target, and trades on —
    so an operator told "target reached" would watch the system keep trading.
    """
    await controls(daily_profit_target=2000.0, maximum_daily_risk_percent=10.0)
    await book(realized="0", unrealized="2200", status="OPEN")
    assert await decide() == "Daily profit target reached"

    async with SessionLocal() as session:
        position = await session.scalar(select(PaperPosition).where(PaperPosition.instrument_token == TOKEN))
        position.status, position.open_quantity = "CLOSED", 0
        position.realized_pnl, position.unrealized_pnl = Decimal("800"), Decimal("0")
        position.total_pnl = Decimal("800")
        await session.commit()

    assert await decide() == "Daily profit target reached"


async def test_a_reached_loss_limit_stays_reached_when_the_position_recovers() -> None:
    await controls(daily_loss_limit=1000.0, maximum_daily_risk_percent=10.0)
    await book(realized="0", unrealized="-1100", status="OPEN")
    assert await decide() == "Daily loss limit reached"

    async with SessionLocal() as session:
        position = await session.scalar(select(PaperPosition).where(PaperPosition.instrument_token == TOKEN))
        position.status, position.open_quantity = "CLOSED", 0
        position.realized_pnl, position.unrealized_pnl = Decimal("-200"), Decimal("0")
        position.total_pnl = Decimal("-200")
        await session.commit()

    assert await decide() == "Daily loss limit reached"


async def test_the_halt_records_the_figure_that_tripped_it() -> None:
    """Not the figure now. It is what the operator was told."""
    await controls(daily_profit_target=2000.0, maximum_daily_risk_percent=10.0)
    await book(realized="2400", status="CLOSED")
    await decide()
    async with SessionLocal() as session:
        halt = await session.scalar(
            select(SessionHalt).where(SessionHalt.session_date == SESSION, SessionHalt.mode == "PAPER")
        )
    assert halt is not None
    assert halt.reason == "Daily profit target reached"
    assert Decimal(str(halt.session_pnl)) == Decimal("2400")


async def test_one_halt_per_session_however_many_signals_arrive() -> None:
    await controls(daily_loss_limit=1000.0, maximum_daily_risk_percent=10.0)
    await book(realized="-1500")
    for _ in range(3):
        assert await decide() == "Daily loss limit reached"
    async with SessionLocal() as session:
        halts = list(
            (
                await session.scalars(
                    select(SessionHalt).where(SessionHalt.session_date == SESSION, SessionHalt.mode == "PAPER")
                )
            ).all()
        )
    assert len(halts) == 1


async def test_a_day_that_never_reaches_a_limit_records_no_halt() -> None:
    await controls(daily_loss_limit=1000.0, daily_profit_target=2000.0, maximum_daily_risk_percent=10.0)
    await book(realized="300")
    assert await decide() == "Paper risk reserved"
    async with SessionLocal() as session:
        assert (
            await session.scalar(
                select(SessionHalt).where(SessionHalt.session_date == SESSION, SessionHalt.mode == "PAPER")
            )
            is None
        )


# --- a paper-only strategy keeps its own day ----------------------------------
#
# On 27 November the RS Pullback paper copies lost about 750 by 10:18 AM. On one
# shared paper ledger that ends the paper day, refuses a paper-only strategy's
# 10:10 AM signal and flattens its positions, though it had lost nothing. A
# paper-only strategy is measured as if it traded the account alone.

SIP = "sip-momentum-v1"


@pytest.fixture
def sip_paper_only(monkeypatch):  # noqa: ANN001, ANN201
    from app.services.strategy_registry import StrategyRegistry

    async def _paper_only(_session):  # noqa: ANN001, ANN202
        return (SIP,)

    monkeypatch.setattr(StrategyRegistry, "paper_only_types", _paper_only)


async def retag(version: str) -> None:
    """Move every position and signal booked so far onto ``version``."""
    async with SessionLocal() as session:
        for row in (await session.scalars(select(PaperPosition).where(PaperPosition.instrument_token == TOKEN))).all():
            row.strategy_version = version
        for row in (await session.scalars(select(PaperSignal).where(PaperSignal.instrument_token == TOKEN))).all():
            row.strategy_version = version
        await session.commit()


async def decide_as(version: str) -> str:
    signal = await make_signal()
    async with SessionLocal() as session:
        stored = await session.get(PaperSignal, signal.id)
        stored.strategy_version = version
        await session.commit()
        await session.refresh(stored)
    return (await PaperRiskEngine().reserve_signal(stored)).reason


async def test_the_shared_books_loss_does_not_stop_a_paper_only_strategy(sip_paper_only) -> None:  # noqa: ANN001
    await controls(daily_loss_limit=1000.0, maximum_daily_risk_percent=10.0)
    await book(realized="-1200")
    assert await decide() == "Daily loss limit reached"
    assert await decide_as(f"{SIP}@1") == "Paper risk reserved"


async def test_a_paper_only_strategys_loss_stops_only_itself(sip_paper_only) -> None:  # noqa: ANN001
    await controls(daily_loss_limit=1000.0, maximum_daily_risk_percent=10.0)
    await book(realized="-1200")
    await retag(f"{SIP}@1")
    assert await decide_as(f"{SIP}@1") == "Daily loss limit reached"
    # The shared book has lost nothing and is not halted by it.
    assert await decide() == "Paper risk reserved"
    async with SessionLocal() as session:
        assert await session.scalar(select(SessionHalt).where(SessionHalt.session_date == SESSION)) is None


async def test_the_shared_halt_does_not_flatten_a_paper_only_position(sip_paper_only) -> None:  # noqa: ANN001
    from app.db.models import PaperOrder
    from app.services.paper_execution import PaperOrderManager

    await controls(daily_loss_limit=1000.0, maximum_daily_risk_percent=10.0)
    await book(realized="0", status="OPEN")
    await retag(f"{SIP}@1")
    await book(realized="-1200")
    async with SessionLocal() as session:
        await PaperOrderManager()._halt_if_the_day_is_over(session, SESSION)
        await session.commit()
        halts = (await session.scalars(select(PaperOrder).where(PaperOrder.instrument_token == TOKEN))).all()
        await session.execute(delete(PaperOrder).where(PaperOrder.instrument_token == TOKEN))
        await session.commit()
    # The shared day halted (its -1,200 passed the limit) but the paper-only
    # position, still open, was left to its own stop.
    assert [order.order_role for order in halts] == []
    async with SessionLocal() as session:
        assert await session.scalar(select(SessionHalt).where(SessionHalt.session_date == SESSION)) is not None


async def test_a_paper_only_book_past_its_loss_limit_is_flattened(sip_paper_only) -> None:  # noqa: ANN001
    """Refusing new entries is not enough for a strategy that enters everything at 10:10."""
    from app.db.models import PaperOrder
    from app.services.paper_execution import PaperOrderManager

    await controls(daily_loss_limit=1000.0, maximum_daily_risk_percent=10.0)
    await book(realized="0", unrealized="-1100", status="OPEN")
    await retag(f"{SIP}@1")
    manager = PaperOrderManager()
    try:
        for _ in range(2):  # a second candle must not add a second exit
            async with SessionLocal() as session:
                await manager._halt_if_the_day_is_over(session, SESSION)
                await session.commit()
        async with SessionLocal() as session:
            halts = list(
                (
                    await session.scalars(
                        select(PaperOrder).where(PaperOrder.instrument_token == TOKEN, PaperOrder.order_role == "HALT")
                    )
                ).all()
            )
            shared_halt = await session.scalar(select(SessionHalt).where(SessionHalt.session_date == SESSION))
        assert len(halts) == 1 and halts[0].order_type == "MARKET"
        assert halts[0].simulation_snapshot["reason"] == "Daily loss limit reached"
        # The shared book lost nothing and is not halted by it.
        assert shared_halt is None
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(PaperOrder).where(PaperOrder.instrument_token == TOKEN))
            await session.commit()
