"""Deterministic paper-only order simulation driven exclusively by completed candles."""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import (
    ApplicationSetting,
    MarketCandle,
    MarketIndicatorSnapshot,
    PaperFill,
    PaperOrder,
    PaperPosition,
    PaperSignal,
)
from app.db.session import SessionLocal
from app.services import daily_limits
from app.services.exit_rules import from_controls as exit_rules_from
from app.services.exit_rules import time_exit_due, trail_to
from app.services.live_shadow_runner import run_live_shadow
from app.services.market_calculations import CompletedCandle
from app.services.oms import PaperOmsGateway

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
    # NSE's Investor Protection Fund Trust charge, Rs 10 a crore on both legs.
    ipft_percent: float = Field(default=0.0001, ge=0, le=1)
    # A stop is an SL-M order, which Upstox turns into an SL-limit with this
    # much protection (``upstox_orders.MARKET_PROTECTION_PERCENT``). A candle
    # that opens beyond that band and never trades back inside it does not
    # fill the stop; the order keeps resting at the band's edge. 0 turns the
    # band off and every triggered stop fills.
    stop_limit_band_percent: float = Field(default=2.0, ge=0, le=20)


DEFAULT_PAPER_EXECUTION_CONTROLS = PaperExecutionControls().model_dump()

# Order roles that close a position. One of them filling settles the signal, so
# the others must not also fill on the same candle — which would exit a position
# twice and book the second exit against a quantity that is no longer there.
# TIME is an exit the clock asked for: a per-strategy holding limit or a
# square-off time. It sorts last of the exits because a stop or target resting
# at the exchange would have been hit before anybody squared anything off.
EXIT_ROLES = frozenset({"TARGET", "STOP", "HALT", "TIME"})


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


def transaction_costs(
    price: Decimal,
    quantity: int,
    side: str,
    controls: PaperExecutionControls,
    prior_gross: Decimal = Decimal("0"),
) -> CostBreakdown:
    """Costs for one fill, with the brokerage cap applied across the whole order.

    Every charge except brokerage is a flat percentage, so splitting an order into
    fills leaves them unchanged. Brokerage is not: it is capped per order, and an
    order that fills in eleven slices is still one order to the broker. Charging
    the cap once per fill therefore overstates it by the number of slices — which
    barely shows at a large position size, where the cap binds anyway, and
    dominates every other cost at a small one, where it does not.

    ``prior_gross`` is the value already filled on this order. The brokerage due
    is the cap-limited charge on the cumulative value minus what earlier fills
    were charged, which is correct whether or not the cap has been reached and
    needs no record of the earlier charges.
    """
    gross = price * quantity

    def percentage(rate: float) -> Decimal:
        return gross * Decimal(str(rate)) / Decimal("100")

    def capped_brokerage(value: Decimal) -> Decimal:
        rate = Decimal(str(controls.brokerage_percent)) / Decimal("100")
        return min(value * rate, Decimal(str(controls.brokerage_cap)))

    brokerage = capped_brokerage(prior_gross + gross) - capped_brokerage(prior_gross)
    stt = percentage(controls.stt_sell_percent) if side == "SELL" else Decimal("0")
    # IPFT is levied by the exchange alongside its transaction charge and is
    # reported with it, so a fill row needs no new column for it.
    exchange_charge = percentage(controls.exchange_transaction_percent) + percentage(controls.ipft_percent)
    sebi_charge = percentage(controls.sebi_percent)
    # GST is charged on brokerage, the exchange's charges and SEBI's fee alike.
    gst = (brokerage + exchange_charge + sebi_charge) * Decimal(str(controls.gst_percent)) / Decimal("100")
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
            oms_order = await PaperOmsGateway().ensure_entry(session, signal)
            session.add(
                PaperOrder(
                    paper_signal_id=signal.id,
                    oms_order_id=oms_order.id,
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
        # After the paper order is durable, and outside its transaction: record
        # what the live path would have decided about this same signal. Reads
        # broker state, submits nothing, and cannot raise into paper execution.
        await run_live_shadow(get_settings(), signal, oms_order.id)

    async def _controls(self, session: AsyncSession) -> PaperExecutionControls:
        setting = await session.get(ApplicationSetting, PAPER_EXECUTION_KEY)
        return PaperExecutionControls.model_validate(setting.value if setting else DEFAULT_PAPER_EXECUTION_CONTROLS)

    @staticmethod
    def _fillable(
        order: PaperOrder, candle: CompletedCandle, band_percent: float = 0.0
    ) -> tuple[bool, Decimal]:
        if order.order_type == "MARKET":
            return True, candle.open
        if order.order_type == "LIMIT" and (
            (order.side == "SELL" and candle.high >= order.limit_price)
            or (order.side == "BUY" and candle.low <= order.limit_price)
        ):
            return True, order.limit_price
        if order.order_type == "STOP":
            return PaperOrderManager._stop_fill(order, candle, band_percent)
        return False, Decimal("0")

    @staticmethod
    def _stop_fill(order: PaperOrder, candle: CompletedCandle, band_percent: float) -> tuple[bool, Decimal]:
        """Where a triggered stop fills, if it fills on this candle at all.

        It used to fill at the stop price whatever the candle did. A share that
        opened 3% through a long's stop was booked out at the stop, a price that
        never traded: every gap made the paper record look better than the
        account would have been. Now:

        - traded through from above (opened on the safe side): the stop price;
        - opened beyond the stop but inside the SL-limit band: the open;
        - opened beyond the band: only if the candle comes back to the band's
          edge, and then at that edge -- otherwise no fill, and the order rests.
        """
        stop = Decimal(str(order.stop_price))
        band = Decimal(str(band_percent)) / Decimal("100")
        if order.side == "SELL":
            if candle.low > stop:
                return False, Decimal("0")
            if candle.open > stop:
                return True, stop
            limit = stop * (Decimal("1") - band)
            if band <= 0 or candle.open >= limit:
                return True, candle.open
            return (True, _money(limit)) if candle.high >= limit else (False, Decimal("0"))
        if candle.high < stop:
            return False, Decimal("0")
        if candle.open < stop:
            return True, stop
        limit = stop * (Decimal("1") + band)
        if band <= 0 or candle.open <= limit:
            return True, candle.open
        return (True, _money(limit)) if candle.low <= limit else (False, Decimal("0"))

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
        # What this order has already filled, so the brokerage cap is applied to
        # the order rather than re-applied to each slice of it.
        prior_gross = (order.average_fill_price or Decimal("0")) * order.filled_quantity
        costs = transaction_costs(price, quantity, order.side, controls, prior_gross)
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
        await PaperOmsGateway().record_fill(session, order)
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
            # ``<= 0`` rather than ``== 0``. The clamp above means a fill can no
            # longer take the quantity past zero, but a position that ever did
            # land there stayed REDUCING for good: the counterpart was never
            # cancelled and the trade was read as still open by every report
            # that asks. A state the system cannot recover from is worth one
            # character to avoid.
            position.status = "CLOSED" if position.open_quantity <= 0 else "REDUCING"
            if position.status == "CLOSED":
                position_closed = True
                position.closed_at = candle.closed_at
                for alternate in list(
                    (
                        await session.scalars(
                            select(PaperOrder).where(
                                PaperOrder.paper_signal_id == signal.id,
                                PaperOrder.id != order.id,
                                # ENTRY as well: a partly filled entry whose
                                # filled part was just stopped out must not
                                # fill the rest on the same or a later candle,
                                # into a position whose stop is already spent.
                                PaperOrder.order_role.in_(["ENTRY", "TARGET", "STOP", "TIME", "HALT"]),
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

    async def _current_atr(self, session: AsyncSession, candle: CompletedCandle) -> Decimal | None:
        """ATR as of this candle, for a trail that follows current volatility.

        Read from the indicator snapshot the aggregation writes per candle
        rather than from the signal, because a trail set from entry-time
        volatility stops adapting exactly when adapting matters — a volatility
        spike after entry is the case an ATR trail exists for.
        """
        row = await session.scalar(
            select(MarketIndicatorSnapshot)
            .where(
                MarketIndicatorSnapshot.instrument_token == candle.instrument_token,
                MarketIndicatorSnapshot.candle_opened_at <= candle.opened_at,
                MarketIndicatorSnapshot.session_date == candle.session_date,
            )
            .order_by(MarketIndicatorSnapshot.candle_opened_at.desc())
            .limit(1)
        )
        value = (row.values or {}).get("atr") if row is not None else None
        try:
            return Decimal(str(value)) if value is not None else None
        except (TypeError, ArithmeticError):
            return None

    async def _manage_open_positions(
        self, session: AsyncSession, candle: CompletedCandle, positions: list[PaperPosition]
    ) -> None:
        """Move the stop and close on the clock, per the rules the trade was taken under.

        The rules come from the signal's snapshot, not from the strategy as it
        stands now. A strategy edited at 11:00 must not move the stop of a
        position opened at 10:30: that trade was taken under the old rules and
        has to be managed — and judged — under them.
        """
        open_positions = [position for position in positions if position.open_quantity > 0]
        if not open_positions:
            return
        atr = await self._current_atr(session, candle)

        for position in open_positions:
            signal = await session.get(PaperSignal, position.paper_signal_id)
            if signal is None:
                continue
            snapshot = signal.strategy_snapshot or {}
            rules = exit_rules_from(snapshot.get("effective_controls") or {})

            await self._trail_stop(session, position, signal, candle, atr, rules)
            reason = time_exit_due(opened_at=position.opened_at, now=candle.closed_at, rules=rules)
            if reason is not None:
                await self._queue_time_exit(session, position, signal, candle, reason)

    async def _trail_stop(self, session, position, signal, candle, atr, rules) -> None:
        stop_order = await session.scalar(
            select(PaperOrder).where(
                PaperOrder.paper_signal_id == signal.id,
                PaperOrder.order_role == "STOP",
                PaperOrder.status.in_(["PENDING", "PARTIALLY_FILLED"]),
            )
        )
        if stop_order is None or stop_order.stop_price is None or position.average_entry_price is None:
            return
        # The risk the trade was sized on, from the signal rather than from the
        # stop as it stands: once the stop has moved, the distance to it is no
        # longer the R that "one R ahead" refers to.
        risk_per_unit = abs(Decimal(str(signal.entry_price)) - Decimal(str(signal.stop_price)))
        moved = trail_to(
            side=position.side,
            entry=position.average_entry_price,
            current_stop=stop_order.stop_price,
            risk_per_unit=risk_per_unit,
            candle_close=candle.close,
            candle_extreme=candle.high if position.side == "LONG" else candle.low,
            atr=atr,
            rules=rules,
        )
        if moved is None:
            return
        previous = Decimal(str(stop_order.stop_price))
        stop_order.stop_price = _money(moved)
        position.stop_price = _money(moved)
        # Written down because a trade that closed at a level nobody chose by
        # hand has to be explicable afterwards from the record alone.
        history = list(stop_order.simulation_snapshot.get("trail", []))
        history.append(
            {
                "rule": rules.trailing_rule,
                "from": str(previous),
                "to": str(_money(moved)),
                "at": candle.closed_at.isoformat(),
            }
        )
        stop_order.simulation_snapshot = {**stop_order.simulation_snapshot, "trail": history}

    async def _queue_time_exit(self, session, position, signal, candle, reason: str) -> None:
        existing = await session.scalar(
            select(PaperOrder.id).where(PaperOrder.paper_signal_id == signal.id, PaperOrder.order_role == "TIME")
        )
        if existing is not None:
            return
        # Eligible from the close of this candle, so it fills at the next
        # candle's open. Filling it on the candle that triggered it would book
        # an exit at a price that had already passed.
        session.add(
            PaperOrder(
                paper_signal_id=signal.id,
                client_order_id=f"paper:{signal.id}:time",
                instrument_token=signal.instrument_token,
                session_date=signal.session_date,
                strategy_version=signal.strategy_version,
                side=exit_side(signal.side),
                order_type="MARKET",
                order_role="TIME",
                quantity=position.open_quantity,
                eligible_after=candle.closed_at,
                simulation_snapshot={"source": "exit_rules", "reason": reason, "paper_only": True},
            )
        )

    async def _halt_if_the_day_is_over(self, session: AsyncSession, session_date: date) -> None:
        """Record the halt once, then flatten and stand down.

        Blocking new entries is not enough on its own. A position left running
        after the loss limit keeps losing: a day stopped at -1,050 whose open
        trade then ran to -1,800 has honoured the letter of a 1,000 limit and
        none of its intent. So the open positions are exited too.

        The exit is a MARKET order rather than an immediate synthetic fill,
        which means it fills on the instrument's next candle at that candle's
        open. That is slower, and it is what actually happens: you cannot leave
        a position at the price you decided to leave it. Booking the exit at the
        current mark would make the journal flatter than the truth, and the
        journal is the evidence this whole system is being judged on.

        The profit target flattens too, symmetrically. A day declared finished
        at +2,200 that drifts to +800 with the position still open has not
        finished; it has only stopped looking.
        """
        # The trading controls, not the execution controls: the limits are the
        # operator's money settings, which live under a different key.
        from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY, TradingControls

        setting = await session.get(ApplicationSetting, TRADING_KEY)
        trading = TradingControls.model_validate(setting.value if setting else DEFAULT_TRADING_CONTROLS)

        # The shared paper book only. A paper-only strategy (live_orders=false)
        # is measured as if it traded the account alone, the same split the
        # risk engine's ``same_book`` makes: its P&L does not end the shared
        # day, and the shared day's halt does not flatten its positions.
        from app.services.risk_engine import strategy_type_of
        from app.services.strategy_registry import StrategyRegistry

        paper_only = await StrategyRegistry.paper_only_types(session)

        def shared(version: str | None) -> bool:
            return strategy_type_of(version) not in paper_only

        day_positions = list(
            (await session.scalars(select(PaperPosition).where(PaperPosition.session_date == session_date))).all()
        )
        session_pnl = sum(
            (Decimal(str(item.total_pnl or 0)) for item in day_positions if shared(item.strategy_version)),
            start=Decimal("0"),
        )
        verdict = await daily_limits.verdict_for(
            session,
            session_date,
            daily_limits.PAPER,
            session_pnl=session_pnl,
            controls=trading,
        )
        # record_halt returns None when nothing was reached, or when the day
        # halted on an earlier candle and was flattened then.
        if await daily_limits.record_halt(session, session_date, daily_limits.PAPER, verdict) is not None:
            await self._flatten(session, session_date, day_positions, shared, verdict.reason, verdict.session_pnl)

        # Each paper-only book against the same limit, on its own money. Without
        # this a paper-only strategy's limit only refused new entries, which for
        # one that enters everything at 10:10 AM refuses nothing. No latch row:
        # session_halts holds one PAPER row a day and it belongs to the shared
        # book. Flattening is idempotent instead -- a position already carrying
        # a HALT order is not given a second one.
        for book in paper_only:

            def in_book(version: str | None, book: str = book) -> bool:
                return strategy_type_of(version) == book

            book_pnl = sum(
                (Decimal(str(item.total_pnl or 0)) for item in day_positions if in_book(item.strategy_version)),
                start=Decimal("0"),
            )
            reason = daily_limits.evaluate(book_pnl, trading)
            if reason is not None:
                await self._flatten(session, session_date, day_positions, in_book, reason, book_pnl)

    async def _flatten(self, session, session_date: date, positions, belongs, reason: str, pnl: Decimal) -> None:  # noqa: ANN001
        """Cancel the book's resting orders and exit its open positions at market."""
        for order in (
            await session.scalars(
                select(PaperOrder).where(
                    PaperOrder.session_date == session_date,
                    PaperOrder.status.in_(["PENDING", "PARTIALLY_FILLED"]),
                )
            )
        ).all():
            if not belongs(order.strategy_version) or order.order_role == "HALT":
                continue
            # A halted day must not open anything new, and the brackets on a
            # position being exited would otherwise race the exit.
            order.status = "CANCELLED"
            order.rejection_reason = reason

        for position in positions:
            if (
                position.status not in {"OPENING", "OPEN", "REDUCING"}
                or position.open_quantity <= 0
                or not belongs(position.strategy_version)
            ):
                continue
            signal = await session.get(PaperSignal, position.paper_signal_id)
            if signal is None:
                continue
            already = await session.scalar(
                select(PaperOrder.id).where(PaperOrder.paper_signal_id == signal.id, PaperOrder.order_role == "HALT")
            )
            if already is not None:
                continue
            session.add(
                PaperOrder(
                    paper_signal_id=signal.id,
                    client_order_id=f"paper:{signal.id}:halt",
                    instrument_token=signal.instrument_token,
                    session_date=session_date,
                    strategy_version=signal.strategy_version,
                    side=exit_side(signal.side),
                    order_type="MARKET",
                    order_role="HALT",
                    quantity=position.open_quantity,
                    eligible_after=position.opened_at or signal.candle_opened_at,
                    simulation_snapshot={
                        "source": "daily_limit_halt",
                        "reason": reason,
                        "session_pnl": str(pnl),
                        "paper_only": True,
                    },
                )
            )

    async def square_off_session(self, session_date: date) -> int:
        """Close every paper position still open after the square-off, at its last price.

        The 3 PM square-off is an order that fills on the next candle. A share
        with no candle after 3 PM -- a quiet name, a feed gap, the scanner
        stopped -- never gets one, and neither does the rest of a TIME order the
        participation cap filled only partly. Such a position would sit open
        overnight in a simulation of an intraday account, and its unrealised
        P&L would stand in the day's total as if it were real. This closes it at
        the last 1-minute close the day recorded, with the usual slippage, and
        says so on the order. Run once a day, late in the session. Returns how
        many positions it closed.
        """
        closed = 0
        settled: list[object] = []
        async with SessionLocal() as session:
            controls = await self._controls(session)
            positions = list(
                (
                    await session.scalars(
                        select(PaperPosition)
                        .where(
                            PaperPosition.session_date == session_date,
                            PaperPosition.status.in_(["OPENING", "OPEN", "REDUCING"]),
                        )
                        .with_for_update()
                    )
                ).all()
            )
            for position in positions:
                signal = await session.get(PaperSignal, position.paper_signal_id)
                if signal is None:
                    continue
                for order in (
                    await session.scalars(
                        select(PaperOrder).where(
                            PaperOrder.paper_signal_id == signal.id,
                            PaperOrder.status.in_(["PENDING", "PARTIALLY_FILLED"]),
                        )
                    )
                ).all():
                    order.status = "CANCELLED"
                    order.rejection_reason = "End-of-day paper square-off"
                if position.open_quantity <= 0:
                    continue
                last = await session.scalar(
                    select(MarketCandle)
                    .where(
                        MarketCandle.instrument_token == position.instrument_token,
                        MarketCandle.session_date == session_date,
                        MarketCandle.timeframe_seconds == 60,
                    )
                    .order_by(MarketCandle.opened_at.desc())
                    .limit(1)
                )
                if last is None:
                    continue
                candle = CompletedCandle(
                    instrument_token=last.instrument_token,
                    timeframe_seconds=last.timeframe_seconds,
                    opened_at=last.opened_at,
                    closed_at=last.closed_at,
                    open=last.close,
                    high=last.close,
                    low=last.close,
                    close=last.close,
                    volume=last.volume,
                    tick_count=last.tick_count,
                )
                order = PaperOrder(
                    paper_signal_id=signal.id,
                    client_order_id=f"paper:{signal.id}:eod",
                    instrument_token=signal.instrument_token,
                    session_date=session_date,
                    strategy_version=signal.strategy_version,
                    side=exit_side(signal.side),
                    order_type="MARKET",
                    order_role="TIME",
                    quantity=position.open_quantity,
                    filled_quantity=0,
                    fee_total=Decimal("0"),
                    eligible_after=last.opened_at,
                    simulation_snapshot={
                        "source": "end_of_day_square_off",
                        "reason": "No fill after the square-off time; closed at the day's last recorded price",
                        "paper_only": True,
                    },
                )
                session.add(order)
                await session.flush()
                if await self._apply_fill(
                    session, order, signal, candle, controls, last.close, position.open_quantity
                ):
                    settled.append(signal.id)
                closed += 1
            await session.commit()
        if settled:
            from app.services.risk_engine import PaperRiskEngine

            for signal_id in settled:
                await PaperRiskEngine().settle_signal(signal_id)
        return closed

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
                        # Locked, because two candles of one instrument can be in
                        # flight at once and an order is the thing they share. On
                        # 16 October the 11:41 and 11:42 candles both found one
                        # TARGET order PENDING, both filled it, and both wrote
                        # filled_quantity = 0 + 5: the order ended up reading as a
                        # single fill while two fills existed and the position had
                        # been moved twice. The second pass then read the position
                        # after the first had committed, so a long of 5 settled at
                        # a net of -5 and never reached the status that cancels the
                        # counterpart. One real completed trade left the record
                        # entirely, and nothing but an alarm said so.
                        #
                        # The lock makes the second pass wait and then see FILLED,
                        # which is what the status test below has always assumed.
                        .with_for_update()
                    )
                ).all()
            )
            # HALT first: it is the day's stop, and an exit that queued behind a
            # re-entry would be an exit that happened after one more trade.
            orders.sort(
                key=lambda order: {"HALT": 0, "STOP": 1, "TARGET": 2, "TIME": 3, "ENTRY": 4}.get(order.order_role, 5)
            )
            processed_exit_signals: set[object] = set()
            settled_signal_ids: set[object] = set()
            for order in orders:
                if order.order_role in EXIT_ROLES and order.paper_signal_id in processed_exit_signals:
                    continue
                # An earlier order on this candle may have settled this one: an
                # exit that closed the position cancels the rest of its entry.
                if order.status not in {"PENDING", "PARTIALLY_FILLED"}:
                    continue
                fillable, reference = self._fillable(order, candle, controls.stop_limit_band_percent)
                if not fillable:
                    continue
                signal = await session.get(PaperSignal, order.paper_signal_id)
                if signal is None:
                    order.status, order.rejection_reason = "REJECTED", "Source paper signal is unavailable"
                    continue
                remaining = order.quantity - order.filled_quantity
                quantity = min(remaining, fill_capacity(candle.volume, controls.participation_percent))
                if order.order_role in EXIT_ROLES:
                    # Belt and braces behind the lock. An exit that closes more
                    # than is open is not a worse fill, it is a quantity that
                    # never existed: the position goes net short of itself, the
                    # status never reaches CLOSED, and the trade disappears from
                    # every total while its charges stay in the day's. Bounded
                    # here so the worst any future race can do is fill nothing.
                    held = await session.scalar(
                        select(PaperPosition.open_quantity)
                        .where(PaperPosition.paper_signal_id == order.paper_signal_id)
                        .with_for_update()
                    )
                    if held is not None:
                        quantity = min(quantity, max(0, held))
                if quantity > 0:
                    if await self._apply_fill(session, order, signal, candle, controls, reference, quantity):
                        settled_signal_ids.add(signal.id)
                    if order.order_role in EXIT_ROLES:
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
            # After the fills, so a position already closed by its stop or
            # target this candle is not trailed or queued for a time exit it
            # will never need.
            await self._manage_open_positions(session, candle, positions)
            # Checked on every candle rather than only when a signal arrives: a
            # daily limit is normally crossed by a price moving, and on a day
            # that then halts, the next signal never comes — so nothing would
            # ever notice.
            await self._halt_if_the_day_is_over(session, candle.session_date)
            await session.commit()
        if settled_signal_ids:
            from app.services.risk_engine import PaperRiskEngine

            risk_engine = PaperRiskEngine()
            for signal_id in settled_signal_ids:
                await risk_engine.settle_signal(signal_id)
