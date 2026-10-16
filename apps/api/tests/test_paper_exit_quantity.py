"""An exit cannot close more than was ever opened.

On 16 October a live JSWSTEEL trade ran correctly at the broker -- four
bought, four sold at the target, the resting stop cancelled -- and the
simulated ledger recorded five in and ten out. The position settled at a net
of -5 with status REDUCING, and because every report asks ``is_open`` before
counting a trade, one real completed winner left the record entirely. Its
charges stayed in the day's totals, so gross and charges were being measured
over different sets of trades.

The cause was not the one-cancels-other, which worked: the stop was cancelled
with "OCO counterpart completed". It was that two candles of one instrument
were in flight at once over an unlocked order row. Both found the TARGET order
PENDING, both filled it for the full quantity, and both wrote
``filled_quantity = 0 + 5`` -- so the order read as a single fill while two
PaperFill rows existed and the position had been moved twice. The second pass
read the position after the first had committed, which is how a long of five
reached minus five.

Three guards now, and these tests pin the two that are reachable from the
public path:

  the order rows are locked while a candle is applied to them, so the second
  pass waits and then sees FILLED

  an exit fill is clamped to what the position actually holds, so the worst a
  future race can do is fill nothing

The third -- marking a position CLOSED at ``<= 0`` rather than exactly zero,
so the counterpart is still cancelled if anything ever lands past zero -- is
defence in depth and the clamp makes it unreachable, which is why there is no
test for it here. It exists because the state it prevents is one the system
could not recover from: REDUCING for good, and invisible to every total.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, select

from app.db.models import PaperFill, PaperOrder, PaperPosition, PaperSignal
from app.db.session import SessionLocal, engine
from app.services.market_calculations import CompletedCandle
from app.services.paper_execution import PaperOrderManager

ENTRY = Decimal("100")
STOP = Decimal("98")
TARGET = Decimal("102")
QUANTITY = 10


def candle(minute: int, *, high: str = "100.5", low: str = "99.5", volume: int = 100) -> CompletedCandle:
    opened_at = datetime(2026, 9, 1, 4, 0, tzinfo=UTC) + timedelta(minutes=minute)
    return CompletedCandle(
        instrument_token="NSE:2885",
        timeframe_seconds=60,
        opened_at=opened_at,
        closed_at=opened_at + timedelta(minutes=1),
        open=Decimal("100"),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal("100"),
        volume=volume,
        tick_count=10,
    )


def a_signal(key: str) -> PaperSignal:
    first = candle(0)
    return PaperSignal(
        signal_key=key,
        instrument_token=first.instrument_token,
        session_date=first.session_date,
        candle_opened_at=first.opened_at,
        strategy_version="orb-retest-v1@1",
        side="LONG",
        entry_price=ENTRY,
        stop_price=STOP,
        target_price=TARGET,
        quantity=QUANTITY,
        risk_amount=Decimal("20"),
        score=100,
        score_breakdown={},
        strategy_snapshot={},
        indicator_snapshot={},
    )


async def opened(key: str) -> PaperSignal:
    """A signal queued and filled, so the position is open and bracketed."""
    signal = a_signal(key)
    async with SessionLocal() as session:
        session.add(signal)
        await session.commit()
        await session.refresh(signal)
    manager = PaperOrderManager()
    await manager.queue_signal(signal)
    # The entry is eligible from the signal candle's close, so the next candle
    # fills it. Kept away from the target so the entry is all that happens.
    await manager.process_completed_candle(candle(1))
    return signal


async def read(signal_id) -> tuple[PaperPosition, dict[str, PaperOrder], list[PaperFill]]:  # noqa: ANN001
    async with SessionLocal() as session:
        position = await session.scalar(select(PaperPosition).where(PaperPosition.paper_signal_id == signal_id))
        orders = list((await session.scalars(select(PaperOrder).where(PaperOrder.paper_signal_id == signal_id))).all())
        fills = list(
            (
                await session.scalars(
                    select(PaperFill)
                    .where(PaperFill.paper_order_id.in_([order.id for order in orders]))
                    .order_by(PaperFill.occurred_at.asc())
                )
            ).all()
        )
    return position, {order.order_role: order for order in orders}, fills


async def clean(key: str) -> None:
    async with SessionLocal() as session:
        await session.execute(delete(PaperSignal).where(PaperSignal.signal_key == key))
        await session.commit()
    await engine.dispose()


def net_of(fills: list[PaperFill], orders: dict[str, PaperOrder]) -> int:
    """What the fills say the position should hold, signed by the order's role."""
    roles = {order.id: role for role, order in orders.items()}
    return sum(fill.quantity if roles.get(fill.paper_order_id) == "ENTRY" else -fill.quantity for fill in fills)


async def test_a_target_crossed_on_two_candles_running_fills_once() -> None:
    """The regression. Price above the target on two consecutive candles is
    ordinary -- a target that fills is usually still crossed a minute later --
    and it must not be two exits."""
    key = "test-exit-quantity-twice"
    try:
        signal = await opened(key)
        manager = PaperOrderManager()
        # Both candles cross the target. The first fills it; the second must
        # find nothing left to do.
        await manager.process_completed_candle(candle(2, high="103"))
        await manager.process_completed_candle(candle(3, high="103"))

        position, orders, fills = await read(signal.id)
        assert position.open_quantity == 0
        assert position.status == "CLOSED"
        assert orders["TARGET"].filled_quantity == QUANTITY
        assert orders["STOP"].status == "CANCELLED"
        assert orders["STOP"].rejection_reason == "OCO counterpart completed"
        # One entry fill and one exit fill. Two exit fills is the whole defect.
        assert len(fills) == 2
    finally:
        await clean(key)


async def test_the_fills_and_the_position_agree() -> None:
    """The invariant the defect broke, stated plainly: the position holds
    exactly what its fills moved it by. A report that trusts open_quantity is
    trusting this."""
    key = "test-exit-quantity-agree"
    try:
        signal = await opened(key)
        manager = PaperOrderManager()
        await manager.process_completed_candle(candle(2, high="103"))
        await manager.process_completed_candle(candle(3, high="103"))

        position, orders, fills = await read(signal.id)
        assert net_of(fills, orders) == position.open_quantity == 0
    finally:
        await clean(key)


async def test_an_exit_cannot_fill_against_a_position_that_holds_nothing() -> None:
    """The clamp, tested against the exact state the race produced: a flat
    position with an exit order still PENDING behind it.

    Before the clamp this filled for the full quantity and took the position
    to minus ten. The account cannot be short a position it never held, and a
    ledger that says so loses the trade from every total.
    """
    key = "test-exit-quantity-clamp"
    try:
        signal = await opened(key)
        # Flatten the position by hand and leave the bracket alive. This is the
        # broken precondition, reproduced rather than raced for: the first pass
        # has closed the position and the second is about to fill the exit.
        async with SessionLocal() as session:
            position = await session.scalar(select(PaperPosition).where(PaperPosition.paper_signal_id == signal.id))
            position.open_quantity = 0
            await session.commit()

        await PaperOrderManager().process_completed_candle(candle(2, high="103"))

        position, orders, fills = await read(signal.id)
        assert position.open_quantity == 0, "an exit must never take a position below what it holds"
        assert orders["TARGET"].filled_quantity == 0
        assert len(fills) == 1, "only the entry fill should exist"
    finally:
        await clean(key)


async def test_a_stop_is_clamped_the_same_way_as_a_target() -> None:
    """Both brackets go through the same path, and a stop is the one whose
    double fill would read as a loss that never happened."""
    key = "test-exit-quantity-clamp-stop"
    try:
        signal = await opened(key)
        async with SessionLocal() as session:
            position = await session.scalar(select(PaperPosition).where(PaperPosition.paper_signal_id == signal.id))
            position.open_quantity = 0
            await session.commit()

        await PaperOrderManager().process_completed_candle(candle(2, low="97"))

        position, orders, fills = await read(signal.id)
        assert position.open_quantity == 0
        assert orders["STOP"].filled_quantity == 0
        assert len(fills) == 1
    finally:
        await clean(key)
