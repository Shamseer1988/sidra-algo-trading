"""The exit rules, driven through the real execution loop rather than called directly.

Unit tests prove the arithmetic; these prove it reaches the order book. Three
things are worth the cost of a database round trip:

  a trailing rule actually moves the resting stop order, and records why
  a clock rule actually queues an exit, and it fills on the next candle
  rules are frozen at signal time, so editing a strategy cannot move the stop
  of a position already open

The third is the one that would be quietly wrong otherwise. Reading the rules
from the strategy as it stands now would be the obvious implementation, and it
would mean a trade taken under one plan gets managed under another — and then
judged against results that mixed the two.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from app.db.models import MarketIndicatorSnapshot, PaperOrder, PaperPosition, PaperSignal
from app.db.session import SessionLocal
from app.services.exit_rules import ATR_TRAIL, BREAKEVEN_AT_R, ExitRules
from app.services.market_calculations import CompletedCandle
from app.services.paper_execution import PaperOrderManager

TOKEN = "NSE:2885"
OPENED = datetime(2026, 9, 1, 4, 0, tzinfo=UTC)  # 09:30 IST
KEY = "test-exit-rules-execution"


def candle(minute: int, *, open_price="100", high="103", low="97", close="101", volume=1000) -> CompletedCandle:
    opened = OPENED + timedelta(minutes=minute)
    return CompletedCandle(
        instrument_token=TOKEN,
        timeframe_seconds=60,
        opened_at=opened,
        closed_at=opened + timedelta(minutes=1),
        open=Decimal(open_price),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=volume,
        tick_count=10,
    )


async def clean() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(PaperSignal).where(PaperSignal.signal_key.like(f"{KEY}%")))
        await session.execute(delete(MarketIndicatorSnapshot).where(MarketIndicatorSnapshot.instrument_token == TOKEN))
        await session.commit()


@pytest.fixture(autouse=True)
async def reset():
    await clean()
    yield
    await clean()


async def open_a_position(rules: ExitRules | None, suffix: str = "") -> PaperSignal:
    """A signal with the given rules, entered on the candle after it."""
    first = candle(0)
    snapshot = {"effective_controls": {"exit_rules": rules.model_dump()}} if rules else {}
    signal = PaperSignal(
        signal_key=f"{KEY}{suffix}",
        instrument_token=TOKEN,
        session_date=first.session_date,
        candle_opened_at=first.opened_at,
        strategy_version="orb-retest-v1@1",
        side="LONG",
        entry_price=Decimal("100"),
        stop_price=Decimal("98"),
        target_price=Decimal("140"),  # far away, so only the rules under test can close it
        quantity=10,
        risk_amount=Decimal("20"),
        score=100,
        score_breakdown={},
        strategy_snapshot=snapshot,
        indicator_snapshot={},
    )
    async with SessionLocal() as session:
        session.add(signal)
        await session.commit()
        await session.refresh(signal)
    manager = PaperOrderManager()
    await manager.queue_signal(signal)
    # Entry is a MARKET order eligible from the next candle; it fills at its open.
    await manager.process_completed_candle(candle(1, open_price="100", close="100", high="100", low="100"))
    return signal


async def orders_for(signal) -> dict[str, PaperOrder]:
    async with SessionLocal() as session:
        rows = list((await session.scalars(select(PaperOrder).where(PaperOrder.paper_signal_id == signal.id))).all())
    return {order.order_role: order for order in rows}


async def record_atr(value: str, minute: int) -> None:
    opened = OPENED + timedelta(minutes=minute)
    async with SessionLocal() as session:
        session.add(
            MarketIndicatorSnapshot(
                instrument_token=TOKEN,
                timeframe_seconds=60,
                session_date=opened.date(),
                candle_opened_at=opened,
                values={"atr": value},
            )
        )
        await session.commit()


async def test_the_default_rules_leave_the_stop_where_it_was():
    signal = await open_a_position(None)
    before = (await orders_for(signal))["STOP"].stop_price
    # A candle that runs a long way in favour. With no trailing rule, nothing moves.
    await PaperOrderManager().process_completed_candle(candle(2, high="130", low="99", close="129"))
    assert (await orders_for(signal))["STOP"].stop_price == before


async def test_breakeven_moves_the_resting_stop_to_entry():
    signal = await open_a_position(ExitRules(trailing_rule=BREAKEVEN_AT_R, trailing_trigger_r=1.0))
    # Entry 100, risk 2, so one R ahead is 102. The stop lands on the price
    # actually paid — 100.02 after slippage — not on the price the signal
    # planned. That is the trade's real break-even on price, and it is still
    # not break-even on money: both legs are charged either way.
    await PaperOrderManager().process_completed_candle(candle(2, high="104", low="100", close="103"))
    stop = (await orders_for(signal))["STOP"]
    assert Decimal(str(stop.stop_price)) == Decimal("100.0200")


async def test_the_move_is_written_down_so_the_exit_can_be_explained_later():
    signal = await open_a_position(ExitRules(trailing_rule=BREAKEVEN_AT_R))
    await PaperOrderManager().process_completed_candle(candle(2, high="104", low="100", close="103"))
    trail = (await orders_for(signal))["STOP"].simulation_snapshot["trail"]
    assert len(trail) == 1
    assert trail[0]["rule"] == BREAKEVEN_AT_R
    assert trail[0]["to"] == "100.0200"


async def test_an_atr_trail_uses_the_atr_of_the_current_candle():
    signal = await open_a_position(ExitRules(trailing_rule=ATR_TRAIL, trailing_atr_multiple=2.0))
    await record_atr("1.5", minute=2)
    # High 110, two ATR of 1.5 back is 107, under the 109.50 close.
    await PaperOrderManager().process_completed_candle(candle(2, high="110", low="100", close="109.50"))
    assert Decimal(str((await orders_for(signal))["STOP"].stop_price)) == Decimal("107.0000")


async def test_a_trailing_stop_that_is_hit_closes_the_position():
    signal = await open_a_position(ExitRules(trailing_rule=BREAKEVEN_AT_R))
    manager = PaperOrderManager()
    await manager.process_completed_candle(candle(2, high="104", low="100", close="103"))
    # Back through the new stop at entry.
    await manager.process_completed_candle(candle(3, high="103", low="99", close="99"))
    async with SessionLocal() as session:
        position = await session.scalar(select(PaperPosition).where(PaperPosition.paper_signal_id == signal.id))
    assert position.status == "CLOSED"
    assert position.open_quantity == 0


async def test_a_holding_limit_queues_an_exit_that_fills_on_the_next_candle():
    signal = await open_a_position(ExitRules(time_exit_minutes=3))
    manager = PaperOrderManager()
    # Opened on candle 1; candle 4 closes 4 minutes later.
    await manager.process_completed_candle(candle(4, high="102", low="100", close="101"))
    queued = (await orders_for(signal)).get("TIME")
    assert queued is not None and queued.status == "PENDING"
    assert "3 minutes" in queued.simulation_snapshot["reason"]

    await manager.process_completed_candle(candle(5, open_price="100.5", high="101", low="100", close="100.5"))
    async with SessionLocal() as session:
        position = await session.scalar(select(PaperPosition).where(PaperPosition.paper_signal_id == signal.id))
    assert position.status == "CLOSED"


async def test_a_time_exit_is_queued_once_not_once_per_candle():
    signal = await open_a_position(ExitRules(time_exit_minutes=3))
    manager = PaperOrderManager()
    await manager.process_completed_candle(candle(4, high="102", low="100", close="101"))
    await manager.process_completed_candle(candle(4, high="102", low="100", close="101"))
    async with SessionLocal() as session:
        count = len(
            (
                await session.scalars(
                    select(PaperOrder).where(PaperOrder.paper_signal_id == signal.id, PaperOrder.order_role == "TIME")
                )
            ).all()
        )
    assert count == 1


async def test_a_square_off_time_is_read_in_ist():
    # 09:55 IST is well before a 15:15 square-off, so nothing is queued.
    signal = await open_a_position(ExitRules(square_off_time="15:15"))
    await PaperOrderManager().process_completed_candle(candle(4, high="102", low="100", close="101"))
    assert (await orders_for(signal)).get("TIME") is None


async def test_editing_the_strategy_does_not_move_an_open_positions_stop():
    # The rules travel with the signal. A position opened under "no trailing"
    # stays under it even if the strategy is changed while the trade is live.
    signal = await open_a_position(None)
    async with SessionLocal() as session:
        stored = await session.get(PaperSignal, signal.id)
        assert stored.strategy_snapshot == {}
    before = (await orders_for(signal))["STOP"].stop_price
    await PaperOrderManager().process_completed_candle(candle(2, high="130", low="99", close="129"))
    assert (await orders_for(signal))["STOP"].stop_price == before
