"""Deterministic paper-only order simulation driven exclusively by completed candles."""

from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ApplicationSetting, PaperFill, PaperOrder, PaperPosition, PaperSignal
from app.db.session import SessionLocal
from app.services.market_calculations import CompletedCandle

PAPER_EXECUTION_KEY = "paper_execution_controls"
MONEY = Decimal("0.0001")


class PaperExecutionControls(BaseModel):
    """Configurable Indian-cash-equity cost and deterministic fill assumptions."""

    slippage_bps: float = Field(default=2, ge=0, le=100)
    participation_percent: float = Field(default=10, gt=0, le=100)
    brokerage_percent: float = Field(default=0.03, ge=0, le=1)
    brokerage_cap: float = Field(default=20, ge=0, le=1_000)
    stt_sell_percent: float = Field(default=0.025, ge=0, le=1)
    exchange_transaction_percent: float = Field(default=0.00297, ge=0, le=1)
    gst_percent: float = Field(default=18, ge=0, le=100)
    sebi_percent: float = Field(default=0.0001, ge=0, le=1)
    stamp_duty_buy_percent: float = Field(default=0.003, ge=0, le=1)


DEFAULT_PAPER_EXECUTION_CONTROLS = PaperExecutionControls().model_dump()


@dataclass(frozen=True)
class CostBreakdown:
    brokerage: Decimal
    stt: Decimal
    exchange_charge: Decimal
    gst: Decimal
    sebi_charge: Decimal
    stamp_duty: Decimal

    @property
    def total(self) -> Decimal:
        return self.brokerage + self.stt + self.exchange_charge + self.gst + self.sebi_charge + self.stamp_duty


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def slipped_price(reference: Decimal, side: str, slippage_bps: float) -> Decimal:
    impact = reference * Decimal(str(slippage_bps)) / Decimal("10000")
    return _money(reference + impact if side == "BUY" else reference - impact)


def fill_capacity(candle_volume: int, participation_percent: float) -> int:
    return max(1, int(Decimal(max(candle_volume, 0)) * Decimal(str(participation_percent)) / Decimal("100")))


def transaction_costs(price: Decimal, quantity: int, side: str, controls: PaperExecutionControls) -> CostBreakdown:
    gross = price * quantity

    def percentage(rate: float) -> Decimal:
        return gross * Decimal(str(rate)) / Decimal("100")

    brokerage = min(percentage(controls.brokerage_percent), Decimal(str(controls.brokerage_cap)))
    stt = percentage(controls.stt_sell_percent) if side == "SELL" else Decimal("0")
    exchange_charge = percentage(controls.exchange_transaction_percent)
    gst = (brokerage + exchange_charge) * Decimal(str(controls.gst_percent)) / Decimal("100")
    sebi_charge = percentage(controls.sebi_percent)
    stamp_duty = percentage(controls.stamp_duty_buy_percent) if side == "BUY" else Decimal("0")
    return CostBreakdown(*(_money(value) for value in (brokerage, stt, exchange_charge, gst, sebi_charge, stamp_duty)))


def entry_side(signal_side: str) -> str:
    return "BUY" if signal_side == "LONG" else "SELL"


def exit_side(signal_side: str) -> str:
    return "SELL" if signal_side == "LONG" else "BUY"


class PaperOrderManager:
    """Creates and fills simulation records; it does not know about brokers or credentials."""

    async def queue_signal(self, signal: PaperSignal) -> None:
        async with SessionLocal() as session:
            existing = await session.scalar(
                select(PaperOrder.id).where(PaperOrder.paper_signal_id == signal.id, PaperOrder.order_role == "ENTRY")
            )
            if existing:
                return
            session.add(
                PaperOrder(
                    paper_signal_id=signal.id,
                    client_order_id=f"paper:{signal.id}:entry",
                    instrument_token=signal.instrument_token,
                    session_date=signal.session_date,
                    strategy_version=signal.strategy_version,
                    side=entry_side(signal.side),
                    order_type="MARKET",
                    order_role="ENTRY",
                    quantity=signal.quantity,
                    eligible_after=signal.candle_opened_at + timedelta(minutes=1),
                    simulation_snapshot={"source": "scanner_signal", "paper_only": True},
                )
            )
            await session.commit()

    async def _controls(self, session: AsyncSession) -> PaperExecutionControls:
        setting = await session.get(ApplicationSetting, PAPER_EXECUTION_KEY)
        return PaperExecutionControls.model_validate(setting.value if setting else DEFAULT_PAPER_EXECUTION_CONTROLS)

    @staticmethod
    def _fillable(order: PaperOrder, candle: CompletedCandle) -> tuple[bool, Decimal]:
        if order.order_type == "MARKET":
            return True, candle.open
        if order.order_type == "LIMIT" and (
            (order.side == "SELL" and candle.high >= order.limit_price)
            or (order.side == "BUY" and candle.low <= order.limit_price)
        ):
            return True, order.limit_price
        if order.order_type == "STOP" and (
            (order.side == "SELL" and candle.low <= order.stop_price)
            or (order.side == "BUY" and candle.high >= order.stop_price)
        ):
            return True, order.stop_price
        return False, Decimal("0")

    async def _create_exit_orders(self, session: AsyncSession, position: PaperPosition, signal: PaperSignal) -> None:
        orders = list(
            (
                await session.scalars(
                    select(PaperOrder).where(
                        PaperOrder.paper_signal_id == signal.id,
                        PaperOrder.order_role.in_(["TARGET", "STOP"]),
                    )
                )
            ).all()
        )
        by_role = {order.order_role: order for order in orders}
        for role, order_type, price in (("TARGET", "LIMIT", signal.target_price), ("STOP", "STOP", signal.stop_price)):
            order = by_role.get(role)
            if order is None:
                session.add(
                    PaperOrder(
                        paper_signal_id=signal.id,
                        client_order_id=f"paper:{signal.id}:{role.lower()}",
                        instrument_token=signal.instrument_token,
                        session_date=signal.session_date,
                        strategy_version=signal.strategy_version,
                        side=exit_side(signal.side),
                        order_type=order_type,
                        order_role=role,
                        quantity=position.open_quantity,
                        limit_price=price if role == "TARGET" else None,
                        stop_price=price if role == "STOP" else None,
                        eligible_after=position.opened_at or signal.candle_opened_at,
                        simulation_snapshot={"source": "paper_bracket", "paper_only": True},
                    )
                )
            elif order.status in {"PENDING", "PARTIALLY_FILLED"}:
                order.quantity = max(order.filled_quantity, position.open_quantity)

    async def _apply_fill(
        self,
        session: AsyncSession,
        order: PaperOrder,
        signal: PaperSignal,
        candle: CompletedCandle,
        controls: PaperExecutionControls,
        reference_price: Decimal,
        quantity: int,
    ) -> bool:
        price = slipped_price(reference_price, order.side, controls.slippage_bps)
        costs = transaction_costs(price, quantity, order.side, controls)
        previous_quantity = order.filled_quantity
        order.filled_quantity += quantity
        order.average_fill_price = _money(
            ((order.average_fill_price or Decimal("0")) * previous_quantity + price * quantity) / order.filled_quantity
        )
        order.fee_total = _money(Decimal(str(order.fee_total)) + costs.total)
        order.status = "FILLED" if order.filled_quantity == order.quantity else "PARTIALLY_FILLED"
        session.add(
            PaperFill(
                paper_order_id=order.id,
                fill_key=f"{order.client_order_id}:{candle.opened_at.isoformat()}:{order.filled_quantity}",
                instrument_token=order.instrument_token,
                side=order.side,
                quantity=quantity,
                price=price,
                gross_value=_money(price * quantity),
                slippage_amount=_money(abs(price - reference_price) * quantity),
                brokerage=costs.brokerage,
                stt=costs.stt,
                exchange_charge=costs.exchange_charge,
                gst=costs.gst,
                sebi_charge=costs.sebi_charge,
                stamp_duty=costs.stamp_duty,
                total_fees=costs.total,
                occurred_at=candle.closed_at,
            )
        )
        position = await session.scalar(select(PaperPosition).where(PaperPosition.paper_signal_id == signal.id))
        position_closed = False
        if order.order_role == "ENTRY":
            if position is None:
                position = PaperPosition(
                    paper_signal_id=signal.id,
                    instrument_token=signal.instrument_token,
                    session_date=signal.session_date,
                    strategy_version=signal.strategy_version,
                    side=signal.side,
                    initial_quantity=signal.quantity,
                    open_quantity=0,
                    stop_price=signal.stop_price,
                    target_price=signal.target_price,
                    realized_pnl=Decimal("0"),
                    unrealized_pnl=Decimal("0"),
                    fees_total=Decimal("0"),
                    total_pnl=Decimal("0"),
                    opened_at=candle.closed_at,
                )
                session.add(position)
            old_quantity = position.open_quantity
            position.open_quantity += quantity
            position.average_entry_price = _money(
                ((position.average_entry_price or Decimal("0")) * old_quantity + price * quantity)
                / position.open_quantity
            )
            position.current_price = candle.close
            position.status = "OPEN" if order.status == "FILLED" else "OPENING"
            position.fees_total = _money(Decimal(str(position.fees_total)) + costs.total)
            await self._create_exit_orders(session, position, signal)
        elif position is not None:
            closed_before = position.initial_quantity - position.open_quantity
            position.average_exit_price = _money(
                ((position.average_exit_price or Decimal("0")) * closed_before + price * quantity)
                / (closed_before + quantity)
            )
            gross_pnl = (price - position.average_entry_price) * quantity
            if position.side == "SHORT":
                gross_pnl = -gross_pnl
            position.realized_pnl = _money(Decimal(str(position.realized_pnl)) + gross_pnl)
            position.open_quantity -= quantity
            position.current_price = candle.close
            position.fees_total = _money(Decimal(str(position.fees_total)) + costs.total)
            position.status = "CLOSED" if position.open_quantity == 0 else "REDUCING"
            if position.status == "CLOSED":
                position_closed = True
                position.closed_at = candle.closed_at
                for alternate in list(
                    (
                        await session.scalars(
                            select(PaperOrder).where(
                                PaperOrder.paper_signal_id == signal.id,
                                PaperOrder.id != order.id,
                                PaperOrder.order_role.in_(["TARGET", "STOP"]),
                                PaperOrder.status.in_(["PENDING", "PARTIALLY_FILLED"]),
                            )
                        )
                    ).all()
                ):
                    alternate.status = "CANCELLED"
                    alternate.rejection_reason = "OCO counterpart completed"
        if position is not None:
            self._mark_to_market(position, candle.close)
        return position_closed

    @staticmethod
    def _mark_to_market(position: PaperPosition, price: Decimal) -> None:
        position.current_price = price
        if position.open_quantity and position.average_entry_price is not None:
            difference = (price - position.average_entry_price) * position.open_quantity
            position.unrealized_pnl = _money(difference if position.side == "LONG" else -difference)
        else:
            position.unrealized_pnl = Decimal("0")
        position.total_pnl = _money(
            Decimal(str(position.realized_pnl))
            + Decimal(str(position.unrealized_pnl))
            - Decimal(str(position.fees_total))
        )

    async def process_completed_candle(self, candle: CompletedCandle) -> None:
        async with SessionLocal() as session:
            controls = await self._controls(session)
            orders = list(
                (
                    await session.scalars(
                        select(PaperOrder)
                        .where(
                            PaperOrder.instrument_token == candle.instrument_token,
                            PaperOrder.session_date == candle.session_date,
                            PaperOrder.status.in_(["PENDING", "PARTIALLY_FILLED"]),
                            PaperOrder.eligible_after <= candle.opened_at,
                        )
                        .order_by(PaperOrder.order_role.desc(), PaperOrder.created_at)
                    )
                ).all()
            )
            orders.sort(key=lambda order: {"STOP": 0, "TARGET": 1, "ENTRY": 2}.get(order.order_role, 3))
            processed_exit_signals: set[object] = set()
            settled_signal_ids: set[object] = set()
            for order in orders:
                if order.order_role in {"TARGET", "STOP"} and order.paper_signal_id in processed_exit_signals:
                    continue
                fillable, reference = self._fillable(order, candle)
                if not fillable:
                    continue
                signal = await session.get(PaperSignal, order.paper_signal_id)
                if signal is None:
                    order.status, order.rejection_reason = "REJECTED", "Source paper signal is unavailable"
                    continue
                remaining = order.quantity - order.filled_quantity
                quantity = min(remaining, fill_capacity(candle.volume, controls.participation_percent))
                if quantity > 0:
                    if await self._apply_fill(session, order, signal, candle, controls, reference, quantity):
                        settled_signal_ids.add(signal.id)
                    if order.order_role in {"TARGET", "STOP"}:
                        processed_exit_signals.add(order.paper_signal_id)
            positions = list(
                (
                    await session.scalars(
                        select(PaperPosition).where(
                            PaperPosition.instrument_token == candle.instrument_token,
                            PaperPosition.session_date == candle.session_date,
                            PaperPosition.status.in_(["OPENING", "OPEN", "REDUCING"]),
                        )
                    )
                ).all()
            )
            for position in positions:
                self._mark_to_market(position, candle.close)
            await session.commit()
        if settled_signal_ids:
            from app.services.risk_engine import PaperRiskEngine

            risk_engine = PaperRiskEngine()
            for signal_id in settled_signal_ids:
                await risk_engine.settle_signal(signal_id)
