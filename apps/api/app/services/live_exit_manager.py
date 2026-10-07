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
from app.services.exit_rules import (
    BREAKEVEN_AT_R,
    NO_TRAIL,
    minutes_until_square_off,
    time_exit_due,
    trail_to,
    under_account_deadline,
)
from app.services.exit_rules import from_controls as exit_rules_from
from app.services.live_execution_gateway import BrokerNotSelectedError, live_order_adapter
from app.services.live_fills import fills_by_signal
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
from app.services.price_ticks import TICK, round_to_tick
from app.services.trade_counter import LIVE_PLACED_STATUSES, session_bounds_utc
from app.services.trading_calendar import MARKET_TIMEZONE, MarketPhase, TradingCalendar
from app.services.trading_symbols import instrument_tick_size

logger = logging.getLogger(__name__)

# How close to the square-off a protective stop stops being worth placing. A
# stop with less than this to live would be cancelled by the square-off before
# it could do anything, and the broker refuses new intraday stops near the
# close regardless. Five minutes, because the sweep runs every minute and that
# leaves several attempts at the close that actually matters.
STOP_IS_POINTLESS_MINUTES = 5.0

# How old a completed candle may be and still be worth measuring a target
# against. Candles are built from ticks, so a minute with no trade in it
# produces no candle at all and a small gap is ordinary on a quiet instrument.
# A gap this large is not quiet, it is a feed that has stopped, and a target
# judged against a price from twenty minutes ago is a decision made about a
# market that has moved on.
TARGET_PRICE_MAX_AGE_MINUTES = 5.0

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
    # Round trips the broker's book shows as finished and nobody has been told
    # about. Collected here rather than announced here, because this module
    # closes positions and the scheduler is what talks to people.
    closed: list = field(default_factory=list)
    day_pnl: Decimal | None = None

    @property
    def noteworthy(self) -> list[PositionExit]:
        """Anything the operator needs to read. A quiet sweep says nothing."""
        return [
            item
            for item in self.exits
            if item.acted
            or item.step in {"cancel_failed", "orphaned", "no_signal", "stale_price", "trail_blocked", "trail_failed"}
        ]


def _decimal(value) -> Decimal | None:  # noqa: ANN001
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def target_reached(*, long: bool, close: Decimal, target: Decimal) -> bool:
    """A long takes profit at or above its target; a short at or below."""
    return close >= target if long else close <= target


async def _latest_candle(session: AsyncSession, instrument_token: str) -> MarketCandle | None:
    """The newest completed candle, whole.

    Whole rather than just the close, because the close alone cannot say
    whether it is still true and a trail needs the candle's extreme as well.
    This read had no freshness check at all: a feed that stopped at 11:00 left
    the 14:30 target measured against an 11:00 close, which is how a target is
    missed on a trade that reached it or hit on one that did not.
    """
    return await session.scalar(
        select(MarketCandle)
        .where(MarketCandle.instrument_token == instrument_token)
        .order_by(MarketCandle.closed_at.desc())
        .limit(1)
    )


async def _entry_fill(session: AsyncSession, signal: PaperSignal, side: str) -> Decimal | None:
    """What the broker actually paid to open this trade, or None.

    The real fill, not the signal's intended price. Break-even means the price
    paid: a long filled at ₹4,788.80 on a signal that said ₹4,780 and then
    "moved to break-even" at ₹4,780 would have its stop ₹8.80 *below* cost, and
    a trade stopped there is a loser wearing the name of a scratch.
    """
    fills = await fills_by_signal(session, {signal.id})
    recorded = fills.get(signal.id)
    if recorded is None:
        return None
    return recorded.entry(side).price


def price_refusal(closed_at: datetime | None, now: datetime) -> str | None:
    """Why this price cannot be measured against, or None if it can."""
    if closed_at is None:
        return "there is no completed candle for it"
    stamped = closed_at if closed_at.tzinfo else closed_at.replace(tzinfo=UTC)
    age = (now - stamped).total_seconds() / 60
    if age > TARGET_PRICE_MAX_AGE_MINUTES:
        return f"the last completed candle is {age:.0f} minutes old"
    return None


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


async def _recorded_stops(session: AsyncSession, signal_id, start, end) -> list[LiveOrderSubmission]:  # noqa: ANN001
    """Every stop this system placed for this trade, as our records have it.

    Selected on the canonical order type rather than the stored one. The column
    holds the broker's spelling, and comparing it against SL-M found the stops
    at Upstox and none at Firstock -- and finding none is not a quiet failure
    here: ``_consider`` reads an empty list as "no stop to cancel" and sends the
    exit anyway. The stop is then still live beside it, both can fill, and the
    position ends up reversed rather than flat. That is the one outcome this
    module exists to prevent.

    This is what we were told at placement, which is a different question from
    what is working now. ``_working`` answers that one.
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


def _open_ids(book: list) -> set[str]:
    """The broker order ids that are actually working right now."""
    return {str(order.broker_order_id) for order in book if order.status in OPEN_STATUSES and order.broker_order_id}


def _working(stops: list[LiveOrderSubmission], book: list | None) -> list[LiveOrderSubmission]:
    """The stops the broker's book still shows as open.

    Our own record says ACCEPTED forever: nothing writes a submission back to
    cancelled, and nothing can, because a cancellation we sent and a fill the
    broker took look the same from here afterwards. So a stop cancelled at the
    square-off was still being handed back as resting a minute later, the cancel
    was re-sent, the broker refused it -- an order that is already gone cannot be
    cancelled -- and ``_consider`` read that refusal as "a stop may still be
    live" and sent no exit at all. Every subsequent minute did the same. The one
    case the square-off exists for, an exit that did not go through, was the
    case in which it stopped trying.

    An unreadable book returns everything recorded, which is the conservative
    direction: a stop assumed live is cancelled needlessly, a stop assumed dead
    fills beside the exit.
    """
    if book is None:
        return stops
    open_ids = _open_ids(book)
    return [stop for stop in stops if any(str(number) in open_ids for number in (stop.broker_order_numbers or []))]


def _ids_resting_on(book: list | None, position) -> set[str]:  # noqa: ANN001
    """Every working order at the broker for this instrument, ours or not.

    Our records are not the whole truth about what is resting against a
    position. A stop whose send returned UNKNOWN is deliberately recorded as
    unresolved rather than accepted -- ``live_protection`` says in as many words
    that it "may or may not be resting" -- and is skipped by every query keyed on
    ACCEPTED. A stop placed by hand from the broker's app is not in our tables at
    all. Either one survives a cancellation that only looked at our own rows,
    fills beside the market exit, and leaves the position reversed rather than
    flat.

    So before flattening, everything working on this instrument is cleared,
    whoever placed it and whichever side it is on. A resting order on a position
    we are about to close is either an exit that would double ours or an entry
    that would re-open what we just closed.

    Matched on the symbol, which is the only key the two books share: an order
    row carries the broker's trading name and no instrument token, and both
    books are the same broker's, so the two names agree. Matching our token
    against their symbol is the mistake that made three earlier joins find
    nothing.
    """
    if book is None:
        return set()
    return {
        str(order.broker_order_id)
        for order in book
        if order.status in OPEN_STATUSES and order.broker_order_id and position.identifies(None, order.symbol)
    }


async def _already_finished(adapter: BrokerAdapter, number: str) -> bool:
    """Is this order off the book already?

    Asked of the book rather than of the broker's refusal text. A cancellation
    fails for two quite different reasons -- the order is already gone, or the
    broker would not do it -- and only one of them is a reason to hold the exit
    back. Telling them apart by parsing an error message would be a string
    comparison standing between a position and being closed.

    An unreadable book answers False: unknown is treated as "may still be live",
    which holds the exit rather than sending one beside a working stop.
    """
    try:
        book = await adapter.normalised_orders()
    except Exception as exc:  # noqa: BLE001
        logger.warning("live_exit_manager.recheck_unreadable order=%s error=%s", number, exc)
        return False
    return str(number) not in _open_ids(book)


async def _cancel_orders(adapter: BrokerAdapter, numbers: list[str]) -> tuple[bool, str]:
    """Clear these orders from the book, or report which ones would not clear."""
    problems: list[str] = []
    for number in numbers:
        try:
            cancelled, detail = await adapter.cancel(str(number))
        except Exception as exc:  # noqa: BLE001
            cancelled, detail = False, f"{type(exc).__name__}: {exc}"
        if cancelled:
            continue
        if await _already_finished(adapter, str(number)):
            logger.info("live_exit_manager.cancel_already_finished order=%s", number)
            continue
        problems.append(f"{number}: {detail}")
    if problems:
        return False, "; ".join(problems)
    return True, ""


def _ids_to_clear(position, stops: list[LiveOrderSubmission], book: list | None) -> list[str]:  # noqa: ANN001
    """Everything that must be off the book before an exit is sent."""
    if book is None:
        # Blind. Our own records are all there is, and all of them are treated
        # as possibly live.
        return sorted({str(number) for stop in stops for number in (stop.broker_order_numbers or []) if number})
    ours = {str(number) for stop in _working(stops, book) for number in (stop.broker_order_numbers or []) if number}
    return sorted(_ids_resting_on(book, position) | ours)


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
        # Read live, not from any signal's snapshot. This is the one exit rule
        # that must reach a position already open: it is the broker's deadline,
        # not part of a trade's plan, and an operator who moves it because the
        # broker moved it needs every open position to hear about it.
        deadline = await _account_deadline(session)
        start, end = session_bounds_utc(now.astimezone(MARKET_TIMEZONE).date())
        exits: list[PositionExit] = []

        for record in positions:
            net = record.net_quantity
            if net is None:
                exits.append(PositionExit(record.symbol, False, "unreadable", "Net quantity could not be read."))
                continue
            if net == 0:
                continue
            exits.append(await _consider(session, adapter, record, net, now, start, end, book, deadline))

        # The end of a trade, detected where it actually happens. A stop resting
        # at the broker fills at the exchange: the position goes to zero and the
        # loop above skips a flat row, so the most common way a trade ends was
        # the one way that produced no message at all.
        closed = await _finished_trades(session, book, start, end)
        day_pnl = _day_total(positions)

    return ExitSweepOutcome(True, "swept", f"{len(exits)} position(s) considered.", exits, closed, day_pnl)


def _day_total(positions: list) -> Decimal | None:
    """The account's day P&L, or None if any row is unreadable.

    None rather than a partial sum: a running total missing one position is a
    number an operator would act on and should not.
    """
    if not positions:
        return None
    figures = [record.day_pnl for record in positions]
    if any(value is None for value in figures):
        return None
    return sum(figures, start=Decimal("0"))


async def _finished_trades(session: AsyncSession, book: list | None, start, end) -> list:  # noqa: ANN001
    """Round trips finished at the broker that nobody has been told about.

    Never raises: a reporting failure must not stop the sweep that closes
    positions.
    """
    if book is None:
        return []
    try:
        from app.services.live_trade_alerts import closed_trades, todays_submissions, unannounced

        submissions = await todays_submissions(session, start, end)
        return await unannounced(session, closed_trades(submissions, book))
    except Exception:  # noqa: BLE001 - closing positions matters more than reporting them
        logger.exception("live_exit_manager.closed_trade_scan_failed")
        return []


async def _account_deadline(session: AsyncSession) -> str | None:
    """The account's "be flat by" time, or None if it cannot be read.

    None rather than a raise, and None rather than a guess: a sweep that
    refused to run because a settings row would not parse is a sweep that
    closes nothing all afternoon, and a made-up deadline is a square-off at a
    time nobody chose.
    """
    try:
        from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY, TradingControls
        from app.db.models import ApplicationSetting

        row = await session.get(ApplicationSetting, TRADING_KEY)
        controls = TradingControls.model_validate(row.value if row else DEFAULT_TRADING_CONTROLS)
        return controls.session_square_off_time
    except Exception:  # noqa: BLE001
        logger.warning("live_exit_manager.account_deadline_unreadable")
        return None


async def _hold_or_protect(
    session: AsyncSession,
    adapter: BrokerAdapter,
    position,  # noqa: ANN001 - BrokerPositionRecord
    signal: PaperSignal,
    net: Decimal,
    symbol: str,
    quantity: int,
    price_note: str,
    start,  # noqa: ANN001
    end,  # noqa: ANN001
    book: list | None,
    rules=None,  # noqa: ANN001 - ExitRules; already resolved against the account deadline
    now: datetime | None = None,
    candle: MarketCandle | None = None,
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
    holding = PositionExit(symbol, False, "holding", f"Holding; {price_note}.", quantity=quantity)
    if book is None:
        # Unreadable book. Saying nothing is better than placing a second stop
        # behind a position that already has one: both would fill and the
        # position would end up reversed rather than flat.
        return holding

    ours = await _recorded_stops(session, signal.id, start, end)
    working = _working(ours, book)
    # How much of the position is actually covered, not merely whether a stop
    # exists. The check used to stop at "any working stop" and return, so a stop
    # for 1 behind a position of 2 -- which is what a partially filled entry
    # leaves -- read as protected, once a minute, all day.
    covered = sum(int(stop.quantity or 0) for stop in working)
    if covered >= quantity:
        # Protected. The only thing left to ask is whether the stop should now
        # be somewhere better than it is.
        moved = await _trail_stop(
            session, adapter, position, signal, net, symbol, quantity, candle, working, book, rules
        )
        return moved if moved is not None else holding

    logger.warning(
        "live_exit_manager.position_unprotected symbol=%s qty=%s covered=%s stops_recorded=%s",
        symbol,
        quantity,
        covered,
        len(ours),
    )
    shortfall = quantity - covered
    gap = (
        f"{quantity} of {symbol} was open with no working stop behind it"
        if covered == 0
        else f"{quantity} of {symbol} was open with a working stop covering only {covered}"
    )

    # Near the deadline a stop is the wrong instrument. It would be cancelled
    # within minutes by the square-off that is about to run, it protects almost
    # nothing in the meantime, and the broker stops accepting new intraday
    # stops before the close in any case -- on 6 October Upstox refused one at
    # 15:10 with "the Intraday Order window for the segment is currently closed
    # for the day", and the watchdog had nothing else to try. Closing is what
    # protection means this late.
    left = minutes_until_square_off(rules, now) if (rules is not None and now is not None) else None
    if left is not None and left <= STOP_IS_POINTLESS_MINUTES:
        # The whole position is closed here, so anything resting against it has
        # to come off the book first. When this branch could only be reached
        # with no stop at all there was nothing to cancel; a stop covering part
        # of the position is a stop that would fill beside this exit and reverse
        # what is left.
        cleared, problem = await _cancel_orders(adapter, _ids_to_clear(position, ours, book))
        if not cleared:
            return PositionExit(
                symbol,
                False,
                "cancel_failed",
                f"{gap} with {left:.0f} minute(s) to square-off, and the resting orders could not be "
                f"cleared ({problem}). No exit was sent; close this by hand.",
                quantity=quantity,
            )
        status, detail = await _market_exit(
            session,
            adapter,
            instrument_token=signal.instrument_token,
            side=SELL if net > 0 else BUY,
            quantity=quantity,
            product=(ours[0].canonical_product if ours else None) or INTRADAY,
            paper_signal_id=signal.id,
        )
        return PositionExit(
            symbol,
            status == ACCEPTED,
            "unprotected_closing",
            f"{gap}, with {left:.0f} minute(s) to square-off, so it was closed rather than stopped. {detail}".strip(),
            quantity=quantity,
        )

    # A stop for the uncovered part, not a replacement for the whole position.
    # Cancelling the stop that works and placing a bigger one would leave the
    # position with nothing behind it for as long as that takes; two stops that
    # together match the position close it between them whichever fills first.
    uncovered = Decimal(shortfall) if net > 0 else Decimal(-shortfall)
    outcome = await protect_position(
        session,
        adapter,
        signal=signal,
        net=uncovered,
        product=(ours[0].canonical_product if ours else None) or INTRADAY,
        symbol=symbol,
    )
    return PositionExit(
        symbol,
        outcome.protected or outcome.flattened,
        "unprotected",
        f"{gap}. {outcome.detail}",
        quantity=quantity,
    )


def _resting_trigger(working: list[LiveOrderSubmission], book: list | None) -> Decimal | None:
    """Where the one working stop sits, as the broker has it.

    The broker's book, not our record: our row says where the stop was placed,
    and the whole point of a trail is that it no longer is. Returns None when
    there is more than one working stop or the trigger cannot be read, because
    a trail that guesses the current level either moves a stop backwards or
    cancels and replaces an unchanged one every minute for the rest of the day.
    """
    if book is None or len(working) != 1:
        return None
    numbers = {str(number) for number in (working[0].broker_order_numbers or []) if number}
    triggers = [
        order.trigger_price
        for order in book
        if str(order.broker_order_id) in numbers and order.status in OPEN_STATUSES and order.trigger_price is not None
    ]
    return triggers[0] if len(triggers) == 1 else None


async def _trail_stop(
    session: AsyncSession,
    adapter: BrokerAdapter,
    position,  # noqa: ANN001 - BrokerPositionRecord
    signal: PaperSignal,
    net: Decimal,
    symbol: str,
    quantity: int,
    candle: MarketCandle | None,
    working: list[LiveOrderSubmission],
    book: list | None,
    rules,  # noqa: ANN001 - ExitRules
) -> PositionExit | None:
    """Move the stop up behind a trade that is far enough ahead, or leave it.

    Trailing existed only in the paper engine. The rule was configurable, the
    screen described it, the journal showed trades exiting at the trailed level
    -- and the broker's stop never moved. An operator who turned break-even on
    was measuring one system and trading another.

    **Only break-even is applied live.** ``trail_to`` also offers an ATR trail,
    and that one is deliberately left to paper and backtesting until a stop can
    be modified in place at the broker. Moving a stop here means cancelling the
    resting one and placing another, so a rule that moves the level most
    minutes of the day would spend the session repeatedly leaving the position
    unprotected for the length of a round trip. Break-even moves once per trade.

    Nothing is moved unless the position is already protected, the book says
    exactly where the stop is, and the candle is fresh. Each of those is a way
    of turning a protective order into a worse one.
    """
    if rules is None or rules.trailing_rule == NO_TRAIL or candle is None:
        return None
    if rules.trailing_rule != BREAKEVEN_AT_R:
        # Said in the log rather than to the operator every minute. The place
        # this has to be visible is the strategy screen, where the rule is
        # chosen, and it says so there.
        logger.info("live_exit_manager.trail_not_applied_live symbol=%s rule=%s", symbol, rules.trailing_rule)
        return None

    current = _resting_trigger(working, book)
    if current is None:
        return None

    long = net > 0
    side = "LONG" if long else "SHORT"
    planned, planned_stop = _decimal(signal.entry_price), _decimal(signal.stop_price)
    if planned is None or planned_stop is None:
        return None
    # The risk the trade was sized on, from the signal rather than from the stop
    # as it stands: once the stop has moved, the distance to it is no longer the
    # R that "one R ahead" refers to. The paper engine measures it the same way,
    # and the two have to agree or the journal stops describing the account.
    risk = abs(planned - planned_stop)
    entry = await _entry_fill(session, signal, side)
    if entry is None:
        entry = planned
    if risk <= 0 or entry <= 0:
        return None

    moved = trail_to(
        side=side,
        entry=entry,
        current_stop=current,
        risk_per_unit=risk,
        candle_close=_decimal(candle.close),
        candle_extreme=_decimal(candle.high if long else candle.low),
        atr=None,
        rules=rules,
    )
    if moved is None:
        return None

    exit_side = SELL if long else BUY
    level = round_to_tick(moved, exit_side, tick=await instrument_tick_size(session, signal.instrument_token) or TICK)
    if level is None or level == current:
        # The tick grid put it back where it already is. Cancelling and
        # replacing an identical stop is a naked moment bought for nothing.
        return None
    # Rounding can push a level through the price that trail_to had just cleared.
    # A stop already through the close is filled on the next tick at a price
    # nobody chose.
    close = _decimal(candle.close)
    if close is not None and (level >= close if long else level <= close):
        return None

    # Cancel first, always. Placing the new stop while the old one rests leaves
    # two live stops behind one position, and when one fills the other opens a
    # reversed one.
    cleared, problem = await _cancel_orders(adapter, _ids_to_clear(position, working, book))
    if not cleared:
        return PositionExit(
            symbol,
            False,
            "trail_blocked",
            f"The stop for {symbol} should move from {current} to {level}, but the resting one could not be "
            f"cancelled ({problem}). It stands where it was; the position is still protected.",
            quantity=quantity,
        )

    outcome = await protect_position(
        session,
        adapter,
        signal=signal,
        net=net,
        product=(working[0].canonical_product if working else None) or INTRADAY,
        symbol=symbol,
        stop_price=level,
    )
    if outcome.protected:
        logger.info("live_exit_manager.stop_trailed symbol=%s from=%s to=%s", symbol, current, level)
        return PositionExit(
            symbol,
            True,
            "trailed",
            f"Stop moved from {current} to {level} — entry was {entry}, so this trade can no longer lose.",
            quantity=quantity,
        )
    return PositionExit(
        symbol,
        outcome.flattened,
        "trail_failed",
        f"The stop for {symbol} was cancelled to move it to {level} and the replacement did not rest. {outcome.detail}",
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
    deadline: str | None = None,
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

    rules = under_account_deadline(
        exit_rules_from((signal.strategy_snapshot or {}).get("effective_controls") or {}), deadline
    )
    reason = time_exit_due(opened_at=signal.created_at, now=now, rules=rules)

    if reason is None:
        candle = await _latest_candle(session, signal.instrument_token)
        close = _decimal(candle.close) if candle is not None else None
        target = _decimal(signal.target_price)
        unusable = (
            price_refusal(candle.closed_at if candle else None, now)
            if close is not None
            else ("there is no completed candle for it")
        )
        if target is None:
            unusable = "the trade carries no target"
        # Nothing downstream may read a candle this old. A target judged on it
        # is wrong, and a stop moved on it is worse -- the trail would set a
        # level from a price the market left behind.
        usable_candle = None if unusable is not None else candle

        if unusable is not None:
            # The target cannot be judged. The stop still can, and must: this
            # branch used to return here, so a position whose instrument had
            # stopped producing candles was the one position the protection
            # watchdog never looked at -- the watchdog lives inside
            # _hold_or_protect and nothing else calls it.
            outcome = await _hold_or_protect(
                session,
                adapter,
                position,
                signal,
                net,
                symbol,
                quantity,
                f"{unusable}, so the target is not being measured",
                start,
                end,
                book,
                rules,
                now,
                usable_candle,
            )
            if outcome.step != "holding":
                return outcome
            # Protected, but blind on the target. Said out loud rather than
            # left in the quiet pile: a feed that has stopped means this trade
            # can now only end on its stop or on the clock.
            return PositionExit(
                symbol,
                False,
                "stale_price",
                f"{quantity} of {symbol} is held with a working stop, but {unusable} — "
                "the target cannot be measured until prices resume.",
                quantity=quantity,
            )

        if not target_reached(long=long, close=close, target=target):
            return await _hold_or_protect(
                session,
                adapter,
                position,
                signal,
                net,
                symbol,
                quantity,
                f"last close {close} against target {target}",
                start,
                end,
                book,
                rules,
                now,
                usable_candle,
            )
        reason = f"Target reached at {close}"

    stops = await _recorded_stops(session, signal.id, start, end)
    clearing = _ids_to_clear(position, stops, book)
    if clearing:
        # Logged because this list can hold orders we did not place -- a stop put
        # on by hand, or one whose send returned UNKNOWN -- and an operator
        # whose manual order disappeared deserves to find out why from
        # somewhere other than the broker's app.
        logger.info("live_exit_manager.clearing_book symbol=%s orders=%s", symbol, ",".join(clearing))
    cancelled, problem = await _cancel_orders(adapter, clearing)
    if not cancelled:
        # No second order while anything may still be live: both could fill, and
        # the position would end up reversed rather than flat.
        return PositionExit(
            symbol,
            False,
            "cancel_failed",
            f"{reason}, but a resting order could not be cancelled ({problem}). No exit was sent; close this by hand.",
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
