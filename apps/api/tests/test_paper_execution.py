from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import delete, select

from app.db.models import PaperOrder, PaperPosition, PaperSignal
from app.db.session import SessionLocal, engine
from app.services.market_calculations import CompletedCandle
from app.services.paper_execution import (
    PaperExecutionControls,
    PaperOrderManager,
    fill_capacity,
    slipped_price,
    transaction_costs,
)


def completed_candle(
    *, open_price: str = "100", high: str = "103", low: str = "97", volume: int = 100
) -> CompletedCandle:
    opened_at = datetime(2026, 9, 1, 4, 0, tzinfo=UTC)
    return CompletedCandle(
        instrument_token="NSE:2885",
        timeframe_seconds=60,
        opened_at=opened_at,
        closed_at=opened_at + timedelta(minutes=1),
        open=Decimal(open_price),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal("101"),
        volume=volume,
        tick_count=10,
    )


def test_paper_costs_and_slippage_are_configurable_and_deterministic() -> None:
    controls = PaperExecutionControls(slippage_bps=5, brokerage_percent=0.03, brokerage_cap=20)
    assert slipped_price(Decimal("100"), "BUY", controls.slippage_bps) == Decimal("100.0500")
    assert slipped_price(Decimal("100"), "SELL", controls.slippage_bps) == Decimal("99.9500")
    buy_cost = transaction_costs(Decimal("100"), 100, "BUY", controls)
    sell_cost = transaction_costs(Decimal("100"), 100, "SELL", controls)
    assert buy_cost.total > 0
    assert sell_cost.stt > 0
    assert sell_cost.stamp_duty == 0
    assert transaction_costs(Decimal("100"), 100, "BUY", controls) == buy_cost


def test_paper_fill_capacity_supports_partial_fill_simulation_without_randomness() -> None:
    assert fill_capacity(100, 10) == 10
    assert fill_capacity(3, 10) == 1
    assert fill_capacity(0, 10) == 1


def test_market_limit_and_stop_fill_rules_use_completed_candle_prices_only() -> None:
    candle = completed_candle()
    manager = PaperOrderManager()
    market = SimpleNamespace(order_type="MARKET", side="BUY", limit_price=None, stop_price=None)
    target = SimpleNamespace(order_type="LIMIT", side="SELL", limit_price=Decimal("102"), stop_price=None)
    stop = SimpleNamespace(order_type="STOP", side="SELL", limit_price=None, stop_price=Decimal("98"))
    untouched = SimpleNamespace(order_type="LIMIT", side="BUY", limit_price=Decimal("96"), stop_price=None)

    assert manager._fillable(market, candle) == (True, Decimal("100"))
    assert manager._fillable(target, candle) == (True, Decimal("102"))
    assert manager._fillable(stop, candle) == (True, Decimal("98"))
    assert manager._fillable(untouched, candle) == (False, Decimal("0"))


async def test_paper_manager_creates_a_next_candle_fill_position_and_oco_exits() -> None:
    signal_candle = completed_candle(volume=100)
    signal = PaperSignal(
        signal_key="test-paper-execution-next-candle",
        instrument_token=signal_candle.instrument_token,
        session_date=signal_candle.session_date,
        candle_opened_at=signal_candle.opened_at,
        strategy_version="orb-retest-v1@1",
        side="LONG",
        entry_price=Decimal("100"),
        stop_price=Decimal("98"),
        target_price=Decimal("102"),
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
    manager = PaperOrderManager()
    try:
        await manager.queue_signal(signal)
        next_candle = CompletedCandle(
            **{
                **signal_candle.__dict__,
                "opened_at": signal_candle.opened_at + timedelta(minutes=1),
                "closed_at": signal_candle.closed_at + timedelta(minutes=1),
            }
        )
        await manager.process_completed_candle(next_candle)
        async with SessionLocal() as session:
            position = await session.scalar(select(PaperPosition).where(PaperPosition.paper_signal_id == signal.id))
            orders = list(
                (await session.scalars(select(PaperOrder).where(PaperOrder.paper_signal_id == signal.id))).all()
            )
        assert position is not None and position.status == "OPEN" and position.open_quantity == 10
        assert {order.order_role for order in orders} == {"ENTRY", "TARGET", "STOP"}
        assert next(order for order in orders if order.order_role == "ENTRY").status == "FILLED"
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(PaperSignal).where(PaperSignal.signal_key == signal.signal_key))
            await session.commit()
        await engine.dispose()
