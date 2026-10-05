"""Close a live position on our terms, not the broker's.

Phase 1 put a stop behind every position. That bounds the loss and nothing
else: a winner runs until the stop or until Upstox squares it off at its own
time and its own price, which the journal then cannot match. This closes the
other two ways a trade should end — the target, and the clock.

**Cancel the stop before sending anything else.** A stop is resting at the
broker. Sending a market exit while it is still live risks both filling — the
exit flattening the position and the stop then *opening* one in the opposite
direction. So the stop is cancelled first, and if the cancel fails no exit is
sent at all. That leaves the position open past our cutoff, which is bad; the
alternative is a reversed position nobody asked for, which is worse. The
operator is told, loudly, and decides.

**Exits are not gated by arming.** ``live_activation`` stops new entries. A
disarmed system with an open position still has to be able to close it — that is
exactly the state this module exists for, and refusing here would recreate the
failure that started all of this. Runtime mode is still checked, because a paper
deployment has no live position to manage.

**The target is measured on completed candles, like paper.** Not because it is
more accurate — it is not, a target can be touched and retraced inside a minute
— but because the paper journal and the live journal must answer "why did this
close where it did" the same way. A live exit measured on ticks and a paper exit
measured on candles would diverge for reasons that have nothing to do with the
strategy.

**A position with no signal behind it is left alone.** If we cannot say which
trade a position belongs to, we do not know its target or its square-off time,
and closing it would be acting on a guess. The reconciler already blocks on
exposure it cannot explain; this reports and leaves it.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import LiveOrderSubmission, MarketCandle, PaperSignal
from app.db.session import SessionLocal
from app.services.broker_adapter import BUY, INTRADAY, OPEN_STATUSES, SELL, BrokerAdapter
from app.services.exit_rules import from_controls as exit_rules_from
from app.services.exit_rules import time_exit_due
from app.services.live_execution_gateway import BrokerNotSelectedError, live_order_adapter
from app.services.live_orders import (
    ACCEPTED,
    UNKNOWN,
    LiveOrderRequest,
    SubmissionOutcome,
    apply_outcome,
    new_client_order_id,
    prepare_submission,
    send_prepared_order,
)
from app.services.live_protection import protect_position
from app.services.trade_counter import LIVE_PLACED_STATUSES, session_bounds_utc
from app.services.trading_calendar import MARKET_TIMEZONE, MarketPhase, TradingCalendar

logger = logging.getLogger(__name__)

MARKET = "MARKET"
STOP_MARKET = "SL-M"


@dataclass
class PositionExit:
    """What was decided about one position, in the words the alert will use."""

    symbol: str
    acted: bool
    step: str
    detail: str
    reason: str = ""
    quantity: int = 0


@dataclass
class ExitSweepOutcome:
    ran: bool
    step: str
    detail: str
    exits: list[PositionExit] = field(default_factory=list)

    @property
    def noteworthy(self) -> list[PositionExit]:
        """Anything the operator needs to read. A quiet sweep says nothing."""
        return [item for item in self.exits if item.acted or item.step in {"cancel_failed", "orphaned", "no_signal"}]


def _decimal(value) -> Decimal | None:  # noqa: ANN001
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def target_reached(*, long: bool, close: Decimal, target: Decimal) -> bool:
    """A long takes profit at or above its target; a short at or below."""
    return close >= target if long else close <= target


async def _latest_close(session: AsyncSession, instrument_token: str) -> Decimal | None:
    candle = await session.scalar(
        select(MarketCandle)
        .where(MarketCandle.instrument_token == instrument_token)
        .order_by(MarketCandle.closed_at.desc())
        .limit(1)
    )
    return _decimal(candle.close) if candle is not None else None


async def _signal_for(session: AsyncSession, position, start, end) -> PaperSignal | None:  # noqa: ANN001
    """The trade this position belongs to, by the entry we placed for it today.

    Entries only: a stop carries the same signal, and taking the latest row
    regardless would find the protective order rather than the trade.

    Most recent first. A symbol can be traded more than once in a day -- four
    trades are allowed and only one position is held at a time -- and the
    position open now belongs to the latest entry. Taking the earliest would
    manage the second trade against the first one's target and square-off time.

    Candidates are filtered in Python rather than by a SQL equality on the
    symbol. The position book and the order record name the same instrument
    differently -- "BHARTIARTL" against "NSE_EQ|INE397D01024" -- so the SQL
    match found nothing and every position this system opened was reported as
    having no entry behind it, once a minute, while going unmanaged. The day's
    rows are few, so reading them and matching on the token costs nothing.
    """
    rows = await session.scalars(
        select(LiveOrderSubmission)
        .where(
            LiveOrderSubmission.created_at >= start,
            LiveOrderSubmission.created_at < end,
            LiveOrderSubmission.status.in_(LIVE_PLACED_STATUSES),
            LiveOrderSubmission.paper_signal_id.isnot(None),
        )
        .order_by(LiveOrderSubmission.created_at.desc())
    )
    for submission in rows:
        # Entries only, and the stop is excluded by its CANONICAL type. The
        # column holds the broker's spelling -- "SL-M" at Upstox, "SL-MKT" at
        # Firstock -- so the SQL comparison this replaces worked on one broker
        # and matched nothing on the other.
        if submission.canonical_order_type == STOP_MARKET:
            continue
        if position.identifies(submission.instrument_token, submission.trading_symbol):
            return await session.get(PaperSignal, submission.paper_signal_id)
    return None


async def _resting_stops(session: AsyncSession, signal_id, start, end) -> list[LiveOrderSubmission]:  # noqa: ANN001
    """Every stop still resting for this trade.

    Selected on the canonical order type rather than the stored one. The column
    holds the broker's spelling, and comparing it against SL-M found the stops
    at Upstox and none at Firstock -- and finding none is not a quiet failure
    here: ``_consider`` reads an empty list as "no stop to cancel" and sends the
    exit anyway. The stop is then still live beside it, both can fill, and the
    position ends up reversed rather than flat. That is the one outcome this
    module exists to prevent.
    """
    rows = await session.scalars(
        select(LiveOrderSubmission).where(
            LiveOrderSubmission.paper_signal_id == signal_id,
            LiveOrderSubmission.status == ACCEPTED,
            LiveOrderSubmission.created_at >= start,
            LiveOrderSubmission.created_at < end,
        )
    )
    return [row for row in rows.all() if row.canonical_order_type == STOP_MARKET]


async def _cancel_stops(adapter: BrokerAdapter, stops: list[LiveOrderSubmission]) -> tuple[bool, str]:
    """Cancel every resting stop for this position, or report why not."""
    problems: list[str] = []
    for stop in stops:
        for number in stop.broker_order_numbers or []:
            try:
                cancelled, detail = await adapter.cancel(str(number))
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{number}: {type(exc).__name__}: {exc}")
                continue
            if not cancelled:
                problems.append(f"{number}: {detail}")
    if problems:
        return False, "; ".join(problems)
    return True, ""


async def _market_exit(
    session: AsyncSession,
    adapter: BrokerAdapter,
    *,
    instrument_token: str,
    side: str,
    quantity: int,
    product: str,
    paper_signal_id,  # noqa: ANN001
) -> tuple[str, str]:
    request = LiveOrderRequest(
        instrument_token=instrument_token,
        side=side,
        quantity=quantity,
        order_type=MARKET,
        product=product,
        price=Decimal("0"),
    )
    client_order_id = new_client_order_id()
    description = await adapter.describe(request.to_broker_order(client_order_id))
    if not description.resolved:
        return "UNRESOLVED", f"{instrument_token} cannot be named at this broker: {description.detail}"

    record = await prepare_submission(
        session,
        request,
        description,
        broker=adapter.name,
        client_order_id=client_order_id,
        paper_signal_id=paper_signal_id,
    )
    await session.commit()
    await session.refresh(record)
    try:
        outcome = await send_prepared_order(adapter, record, request, description)
    except Exception as exc:  # noqa: BLE001 - it may still have reached the exchange
        logger.exception("live_exit_manager.exit_send_raised client_order_id=%s", client_order_id)
        outcome = SubmissionOutcome(status=UNKNOWN, detail=f"Exit submission raised locally: {exc}")
    apply_outcome(record, outcome)
    await session.commit()
    return outcome.status, outcome.detail


async def sweep_live_exits(settings: Settings, calendar: TradingCalendar) -> ExitSweepOutcome:
    """Close what should be closed. Never raises."""
    try:
        return await _sweep(settings, calendar)
    except Exception as exc:  # noqa: BLE001 - a scheduled job must survive one bad run
        logger.exception("live_exit_manager.sweep_failed")
        return ExitSweepOutcome(False, "error", f"{type(exc).__name__}: {exc}")


async def _sweep(settings: Settings, calendar: TradingCalendar) -> ExitSweepOutcome:
    now = datetime.now(UTC)
    status = calendar.status_at(now)
    if not status.trading_day:
        return ExitSweepOutcome(False, "calendar", f"Not a trading day: {status.reason}")
    if status.phase not in {MarketPhase.OPEN, MarketPhase.PRE_OPEN}:
        return ExitSweepOutcome(False, "closed", f"Exchange is {status.phase}.")
    # Deliberately no activation check. Disarming stops new entries; a position
    # already open still has to be closeable, and that is the whole point here.
    if settings.application_mode != "LIVE" or not settings.live_trading_enabled:
        return ExitSweepOutcome(False, "runtime", f"Runtime is {settings.application_mode}.")

    async with SessionLocal() as session:
        try:
            adapter = await live_order_adapter(settings, session)
        except BrokerNotSelectedError as exc:
            return ExitSweepOutcome(False, "broker", f"No live broker selected: {exc}")
        except Exception as exc:  # noqa: BLE001
            return ExitSweepOutcome(False, "broker", f"Broker unreachable: {type(exc).__name__}: {exc}")

        positions = await adapter.normalised_positions()
        # Read once for the whole sweep, not once per position: the protection
        # check below asks the broker whether our stops are actually working,
        # and the answer is the same book for every row.
        try:
            book = await adapter.normalised_orders()
        except Exception as exc:  # noqa: BLE001
            # A book we cannot read is not a book with no stops in it. Treating
            # it as empty would have this sweep place a second stop behind a
            # position that already has one.
            logger.warning("live_exit_manager.order_book_unreadable error=%s", exc)
            book = None
        start, end = session_bounds_utc(now.astimezone(MARKET_TIMEZONE).date())
        exits: list[PositionExit] = []

        for record in positions:
            net = record.net_quantity
            if net is None:
                exits.append(PositionExit(record.symbol, False, "unreadable", "Net quantity could not be read."))
                continue
            if net == 0:
                continue
            exits.append(await _consider(session, adapter, record, net, now, start, end, book))

    return ExitSweepOutcome(True, "swept", f"{len(exits)} position(s) considered.", exits)


async def _hold_or_protect(
    session: AsyncSession,
    adapter: BrokerAdapter,
    signal: PaperSignal,
    net: Decimal,
    symbol: str,
    quantity: int,
    close: Decimal,
    target: Decimal,
    start,  # noqa: ANN001
    end,  # noqa: ANN001
    book: list | None,
) -> PositionExit:
    """Holding is only safe if something is behind the position.

    ``protect_after_fill`` runs once, in the seconds after the entry. For a long
    time that was the only attempt this system ever made, so a stop refused at
    09:37 -- for a bad tick price, a margin blip, a broker hiccup -- left the
    position naked until the square-off, and nothing scheduled would notice. On
    5 October that turned a ₹100 planned risk into ₹163 and climbing, on a
    position whose stop had simply been rejected.

    The check is against the broker's book rather than our own records, and by
    order id rather than by symbol. Our table says what we were told at
    placement; only the book says whether the order is working *now*. And the
    id is the one key both sides agree on -- matching on a symbol is what made
    three earlier joins compare a trading name against an instrument token and
    quietly find nothing.
    """
    holding = PositionExit(
        symbol, False, "holding", f"Holding; last close {close} against target {target}.", quantity=quantity
    )
    if book is None:
        # Unreadable book. Saying nothing is better than placing a second stop
        # behind a position that already has one: both would fill and the
        # position would end up reversed rather than flat.
        return holding

    ours = await _resting_stops(session, signal.id, start, end)
    working = {str(order.broker_order_id) for order in book if order.status in OPEN_STATUSES and order.broker_order_id}
    for stop in ours:
        if any(str(number) in working for number in (stop.broker_order_numbers or [])):
            return holding

    logger.warning(
        "live_exit_manager.position_unprotected symbol=%s qty=%s stops_recorded=%s",
        symbol,
        quantity,
        len(ours),
    )
    outcome = await protect_position(
        session,
        adapter,
        signal=signal,
        net=net,
        product=(ours[0].canonical_product if ours else None) or INTRADAY,
        symbol=symbol,
    )
    return PositionExit(
        symbol,
        outcome.protected or outcome.flattened,
        "unprotected",
        f"{quantity} of {symbol} was open with no working stop behind it. {outcome.detail}",
        quantity=quantity,
    )


async def _consider(
    session: AsyncSession,
    adapter: BrokerAdapter,
    position,  # noqa: ANN001 - BrokerPositionRecord; imported for typing would be circular at runtime
    net: Decimal,
    now: datetime,
    start,  # noqa: ANN001
    end,  # noqa: ANN001
    book: list | None = None,
) -> PositionExit:
    quantity = int(abs(net))
    long = net > 0
    symbol = position.symbol

    signal = await _signal_for(session, position, start, end)
    if signal is None:
        return PositionExit(
            symbol,
            False,
            "no_signal",
            f"{quantity} open with no entry of ours behind it; target and square-off are unknown.",
            quantity=quantity,
        )

    rules = exit_rules_from((signal.strategy_snapshot or {}).get("effective_controls") or {})
    reason = time_exit_due(opened_at=signal.created_at, now=now, rules=rules)

    if reason is None:
        close = await _latest_close(session, signal.instrument_token)
        target = _decimal(signal.target_price)
        if close is None or target is None:
            return PositionExit(
                symbol, False, "no_price", "No completed candle to measure the target against.", quantity=quantity
            )
        if not target_reached(long=long, close=close, target=target):
            return await _hold_or_protect(
                session, adapter, signal, net, symbol, quantity, close, target, start, end, book
            )
        reason = f"Target reached at {close}"

    stops = await _resting_stops(session, signal.id, start, end)
    cancelled, problem = await _cancel_stops(adapter, stops)
    if not cancelled:
        # No second order while a stop may still be live: both could fill, and
        # the position would end up reversed rather than flat.
        return PositionExit(
            symbol,
            False,
            "cancel_failed",
            f"{reason}, but the resting stop could not be cancelled ({problem}). No exit was sent; close this by hand.",
            reason,
            quantity,
        )

    status, detail = await _market_exit(
        session,
        adapter,
        instrument_token=signal.instrument_token,
        side=SELL if long else BUY,
        quantity=quantity,
        # Canonical, for the same reason the stop is: stops[0].product is
        # the broker's word and describe() would translate it twice.
        product=(stops[0].canonical_product if stops else None) or INTRADAY,
        paper_signal_id=signal.id,
    )
    if status == ACCEPTED:
        return PositionExit(symbol, True, "exited", f"{reason}. Exit sent for {quantity}.", reason, quantity)
    return PositionExit(
        symbol,
        False,
        "orphaned",
        f"{reason}, the stop was cancelled, but the exit did not go through ({detail}). "
        f"{quantity} of {symbol} is now open with nothing behind it.",
        reason,
        quantity,
    )
