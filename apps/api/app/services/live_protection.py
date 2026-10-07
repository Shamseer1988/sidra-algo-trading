"""Put a stop at the broker behind every live position, or close the position.

On the first live day this system opened a real short and had no way to close
it. There was no stop order, no target, no square-off, and the reconciliation
gate — correctly — then refused every further order, including any that would
have exited. The account was protected only by the operator noticing.

Two rules follow from that, and they are why this module exists separately from
``live_execution``.

**An exit is never gated by the rules that govern an entry.** ``submit_live_order``
runs the full gate chain: daily loss limit, reconciliation freshness,
activation, approval mode. Every one of those is a reason not to *open* a
position and none of them is a reason to leave one unprotected. A stop routed
through that chain would be refused exactly when it is needed most — the day the
loss limit trips, or the moment reconciliation blocks. So this module uses the
write-ahead primitives underneath (``prepare_submission`` → commit → send) and
skips the gates deliberately.

**An unprotected position is worse than no position.** If the stop cannot be
placed, the answer is not to leave the position and hope: it is to close it. A
transient API error crystallising a small loss is a far better outcome than an
open position with nothing behind it, which is the state that prompted all of
this. That choice is stated here rather than buried, because it is the one an
operator might reasonably want to overrule.

**Quantity comes from the broker, not from the order.** An entry can partially
fill. Sizing the stop from the order would leave the excess to open a *new*
position in the opposite direction when it triggered, turning a protective order
into an entry nobody asked for. So the position book is read back, and if the
broker's net quantity cannot be parsed no stop is guessed — that is escalated
instead, because a stop for the wrong size is worse than an alert.
"""

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import LiveOrderSubmission, PaperSignal
from app.services.broker_adapter import BUY, INTRADAY, OPEN_STATUSES, SELL, BrokerAdapter
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
from app.services.price_ticks import TICK, round_to_tick
from app.services.trading_symbols import instrument_tick_size

logger = logging.getLogger(__name__)

MARKET = "MARKET"
STOP_MARKET = "SL-M"

# A market entry fills in well under a second, but "well under" is not "before
# the next line runs". Polled rather than assumed, and briefly: a stop that
# arrives ten seconds late is still a stop, one that is never placed is not.
FILL_POLL_ATTEMPTS = 6
FILL_POLL_SECONDS = 1.5

# Retried once. A second failure is treated as a real inability to protect
# rather than bad luck, because the cost of being wrong in that direction is an
# open position with nothing behind it.
STOP_PLACE_ATTEMPTS = 2


@dataclass
class ProtectionOutcome:
    """What now stands behind the position, in terms the alert can state."""

    protected: bool
    step: str
    detail: str
    quantity: int = 0
    stop_order_ids: list[str] | None = None
    flattened: bool = False


def _exit_side(position_is_long: bool) -> str:
    """You close a long by selling and a short by buying."""
    return SELL if position_is_long else BUY


def _net_quantity(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None


async def _held_quantity(adapter: BrokerAdapter, token: str, symbol: str | None) -> Decimal | None:
    """The broker's own net quantity for this position, or None if unreadable.

    Matched on the instrument token, because matching on the symbol did not
    work and the way it failed was silent. An order is placed against
    "NSE_EQ|INE397D01024"; the Upstox position book calls the same position
    "BHARTIARTL". Comparing the two found nothing, and "found nothing" is
    indistinguishable here from "flat" -- so a real 7-share short was read as an
    unfilled order and left with no stop behind it.

    None and zero are kept apart on purpose. Zero means flat, which is safe and
    needs no stop; unreadable means the exposure is unknown, which is the
    opposite, and collapsing them would turn a reason to escalate into a reason
    to do nothing.
    """
    for record in await adapter.normalised_positions():
        if record.identifies(token, symbol):
            return record.net_quantity
    return Decimal("0")


async def _place_exit(
    session: AsyncSession,
    adapter: BrokerAdapter,
    *,
    instrument_token: str,
    side: str,
    quantity: int,
    product: str,
    order_type: str,
    trigger_price: Decimal,
    paper_signal_id: Any,
    tick: Decimal = TICK,
) -> tuple[str, list[str], str]:
    """Write the exit down, commit, then send it. Returns (status, ids, detail).

    The same write-ahead order as an entry, for the same reason: if the process
    dies between the send and the write, an exit order exists at the broker that
    this system has no record of.
    """
    request = LiveOrderRequest(
        instrument_token=instrument_token,
        side=side,
        quantity=quantity,
        order_type=order_type,
        product=product,
        # A market exit carries no price, and the adapter zeroes it for SL-M
        # too; the trigger is what the broker acts on.
        price=Decimal("0"),
        trigger_price=trigger_price,
        tick=tick,
    )
    client_order_id = new_client_order_id()
    description = await adapter.describe(request.to_broker_order(client_order_id))
    if not description.resolved:
        return "UNRESOLVED", [], f"{instrument_token} cannot be named at this broker: {description.detail}"

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
    except Exception as exc:  # noqa: BLE001 - the order may still be live
        logger.exception("live_protection.exit_send_raised client_order_id=%s", client_order_id)
        outcome = SubmissionOutcome(status=UNKNOWN, detail=f"Exit submission raised locally: {exc}")

    apply_outcome(record, outcome)
    await session.commit()
    return outcome.status, list(outcome.broker_order_numbers), outcome.detail


async def protect_after_fill(
    session: AsyncSession,
    settings: Settings,
    adapter: BrokerAdapter,
    submission: LiveOrderSubmission,
) -> ProtectionOutcome:
    """Place a stop behind whatever the entry actually filled. Never raises."""
    try:
        return await _protect(session, settings, adapter, submission)
    except Exception as exc:  # noqa: BLE001
        logger.exception("live_protection.failed client_order_id=%s", submission.client_order_id)
        return ProtectionOutcome(False, "error", f"{type(exc).__name__}: {exc}")


async def _cancel_entry(adapter: BrokerAdapter, submission: LiveOrderSubmission) -> tuple[bool, str]:
    """Cancel whatever is left of this entry order. Never raises.

    Never raises: this runs on the path whose whole job is to leave an account
    safe, and a cancellation that fails is something to report rather than
    something to crash on.

    Cancelling an order that has in fact completed is harmless -- the broker
    refuses it -- and is the right way round. The alternative, leaving a live
    order resting because it *might* have filled, is how an account acquires a
    position nobody is watching.

    Returns whether everything was withdrawn, and the order numbers either way,
    so the caller can say which of the two situations it is reporting.
    """
    numbers = [str(number) for number in (submission.broker_order_numbers or []) if number]
    if not numbers:
        return True, ""

    withdrawn: list[str] = []
    stuck: list[str] = []
    for number in numbers:
        try:
            ok, detail = await adapter.cancel(number)
        except Exception as exc:  # noqa: BLE001
            logger.warning("live_protection.cancel_raised order=%s error=%s", number, exc)
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        (withdrawn if ok else stuck).append(number if ok else f"{number} ({detail})")

    return (not stuck), ("; ".join(stuck) if stuck else ", ".join(withdrawn))


async def _broker_verdict(adapter: BrokerAdapter, submission: LiveOrderSubmission) -> tuple[bool, str]:
    """Is this order still working, and what did the broker say about it?

    A cancellation fails for two quite different reasons and only one of them is
    a hanging risk: the order is already finished, or the broker would not do
    it. Asked of the order book rather than of the refusal text, for the same
    reason the exit manager asks there.

    On 7 October a PAYTM entry was accepted over the API and then refused by the
    exchange. The cancellation that followed failed -- an order that is already
    rejected cannot be cancelled -- and this path reported "it may still be live
    at the broker, cancel it by hand", about an order that no longer existed.
    The broker's own words for the refusal were sitting in the order book,
    unread: "you have entered an invalid order price". The operator found them
    by opening the broker's app.

    Returns (still_working, what the broker said). An unreadable book answers
    "still working", because unknown exposure is the one to escalate.
    """
    numbers = {str(number) for number in (submission.broker_order_numbers or []) if number}
    try:
        book = await adapter.normalised_orders()
    except Exception as exc:  # noqa: BLE001 - this path must not raise
        logger.warning("live_protection.book_unreadable error=%s", exc)
        return True, ""
    rows = [record for record in book if str(record.broker_order_id) in numbers]
    if not rows:
        return False, ""
    working = any(record.status in OPEN_STATUSES for record in rows)
    said = next((record.status_message for record in rows if record.status_message), "")
    state = next((record.status for record in rows if record.status), "")
    return working, f"{state}: {said}".strip(": ").strip()


async def _withdraw_unfilled(adapter: BrokerAdapter, submission: LiveOrderSubmission) -> str:
    """Cancel an entry that produced no position, and say what happened."""
    if not [number for number in (submission.broker_order_numbers or []) if number]:
        return "Nothing filled; there is no position to protect."
    ok, detail = await _cancel_entry(adapter, submission)
    if ok:
        return f"Nothing filled. The resting entry was withdrawn ({detail})."

    working, verdict = await _broker_verdict(adapter, submission)
    if not working:
        # Not a hanging order. The cancellation failed because there was nothing
        # left to cancel, and the broker's reason is the thing worth saying.
        return f"Nothing filled. {adapter.name} had already finished with the entry" + (
            f" — {verdict}." if verdict else ", so there was nothing to withdraw."
        )
    return (
        "Nothing filled, and the entry could not be withdrawn: "
        f"{detail}. It is still working at the broker -- cancel it by hand before "
        "it fills with no stop behind it." + (f" The broker says: {verdict}." if verdict else "")
    )


async def _protect(
    session: AsyncSession,
    settings: Settings,
    adapter: BrokerAdapter,
    submission: LiveOrderSubmission,
) -> ProtectionOutcome:
    if submission.paper_signal_id is None:
        return ProtectionOutcome(False, "no_signal", "Submission carries no signal; no stop price to work from.")

    signal = await session.get(PaperSignal, submission.paper_signal_id)
    if signal is None:
        return ProtectionOutcome(False, "no_signal", "The signal behind this submission is gone.")

    # The token is what the order was placed against and what the position book
    # can be joined on; the symbol is what the alert should say.
    token = submission.instrument_token
    symbol = submission.trading_symbol
    net: Decimal | None = Decimal("0")
    for attempt in range(FILL_POLL_ATTEMPTS):
        net = await _held_quantity(adapter, token, symbol)
        if net is None or net != 0:
            break
        if attempt < FILL_POLL_ATTEMPTS - 1:
            await asyncio.sleep(FILL_POLL_SECONDS)

    if net is None:
        # Deliberately no stop. A stop for a guessed quantity can open a
        # position rather than close one.
        return ProtectionOutcome(
            False,
            "unreadable",
            f"Broker net quantity for {symbol} could not be read. No stop was placed; "
            "check the position by hand immediately.",
        )
    if net == 0:
        # An entry that did not fill is not simply a non-event once entries are
        # priced. A MARKET order that filled nothing is finished, but a LIMIT
        # rests: left alone it can fill an hour later, at a price nobody
        # re-checked, with no stop behind it and no risk budget consulted. The
        # order book is the only thing holding it, so it is withdrawn here.
        return ProtectionOutcome(True, "flat", await _withdraw_unfilled(adapter, submission))

    # A partial fill leaves the rest of the entry resting, and that half was
    # invisible here: the stop below is sized to what filled, the remainder goes
    # on working, and when it fills twenty minutes later the position is larger
    # than the stop behind it. The minute sweep then sees a working stop and
    # holds, because it checked that a stop existed rather than that it covered
    # anything. A limit for 2 that filled 1 could run the second share naked to
    # the square-off.
    #
    # Withdrawn before the stop is placed, so the exposure stops growing first.
    # The quantity is then read again rather than assumed: the remainder can
    # fill while the cancellation is in flight, and a stop sized to the earlier
    # reading would leave the same gap one share smaller.
    ordered = int(submission.quantity or 0)
    if ordered and abs(net) < ordered:
        withdrawn, detail = await _cancel_entry(adapter, submission)
        logger.info(
            "live_protection.partial_entry_withdrawn symbol=%s held=%s ordered=%s ok=%s",
            symbol,
            net,
            ordered,
            withdrawn,
        )
        if not withdrawn:
            # The remainder may still fill. A stop placed now would be the right
            # size for the wrong position, and this is the one case where saying
            # so beats guessing -- exactly as an unreadable quantity does above.
            return ProtectionOutcome(
                False,
                "partial_unwithdrawn",
                f"{abs(net)} of {ordered} filled and the rest of the entry could not be withdrawn "
                f"({detail}). No stop was placed, because the position may still grow. Cancel the "
                "resting entry by hand, then protect what is held.",
                int(abs(net)),
            )
        after = await _held_quantity(adapter, token, symbol)
        if after is None:
            return ProtectionOutcome(
                False,
                "unreadable",
                f"Broker net quantity for {symbol} could not be read after withdrawing the rest of a "
                "partial entry. No stop was placed; check the position by hand immediately.",
            )
        if after == 0:
            return ProtectionOutcome(True, "flat", "The partial fill was closed before a stop was needed.")
        net = after

    # The canonical product, never submission.product. That field holds what
    # the broker was asked for -- "I" at Upstox -- and describe() translates
    # again on the way out, so passing it back raised "Unsupported order field:
    # 'I'" and refused both the stop AND the close that would have covered for
    # it. INTRADAY is the fallback only for rows written before the snapshot
    # carried this, and it is what every order this system places uses.
    return await protect_position(
        session,
        adapter,
        signal=signal,
        net=net,
        product=submission.canonical_product or INTRADAY,
        symbol=symbol,
    )


async def protect_position(
    session: AsyncSession,
    adapter: BrokerAdapter,
    *,
    signal: PaperSignal,
    net: Decimal,
    product: str,
    symbol: str | None = None,
    stop_price: Decimal | None = None,
) -> ProtectionOutcome:
    """Put a stop behind a held position, or close it if one cannot be placed.

    Separate from ``protect_after_fill`` so it can be run again later. That one
    fires once, immediately after the entry, and for a long time it was the only
    attempt this system ever made: a stop rejected at 09:37 left the position
    naked for the rest of the day, with nothing scheduled that would notice. On
    5 October that cost more than twice the planned risk on a trade whose stop
    had simply been refused for an invalid tick price. The minute sweep now
    calls this for any open position with no stop behind it.

    ``net`` is the broker's signed quantity, so the side and size come from what
    is actually held rather than from what was ordered.

    ``stop_price`` overrides the signal's level, for a stop that has since been
    moved. It defaults to the signal's, which is where a trade's first stop
    goes; a trail passes the level it worked out, because re-reading the signal
    would put a moved stop back where it started every time this ran.
    """
    quantity = int(abs(net))
    exit_side = _exit_side(net > 0)
    name = symbol or signal.instrument_token
    # The instrument's own grid, not a segment-wide guess. A stop refused for an
    # invalid trigger leaves a position with nothing behind it, which is the
    # failure this whole module exists to prevent.
    tick = await instrument_tick_size(session, signal.instrument_token) or TICK
    # On the exchange's tick grid before anything is said about it. The request
    # normalises it anyway, but a number announced to the operator that the
    # broker never saw is its own small lie -- and this path announced
    # "Stop at 977.9632" on a day the broker rejected exactly that price.
    stop_price = round_to_tick(
        Decimal(str(stop_price if stop_price is not None else signal.stop_price)), exit_side, tick=tick
    )

    for attempt in range(STOP_PLACE_ATTEMPTS):
        status, ids, detail = await _place_exit(
            session,
            adapter,
            instrument_token=signal.instrument_token,
            side=exit_side,
            quantity=quantity,
            product=product,
            order_type=STOP_MARKET,
            trigger_price=stop_price,
            paper_signal_id=signal.id,
            tick=tick,
        )
        if status == ACCEPTED:
            logger.info(
                "live_protection.stop_placed symbol=%s qty=%s trigger=%s ids=%s", name, quantity, stop_price, ids
            )
            return ProtectionOutcome(True, "stopped", f"Stop at {stop_price}.", quantity, ids)
        if status == UNKNOWN:
            # A stop may or may not be resting. Retrying could leave two, and
            # when one triggers the other opens a reversed position; flattening
            # could do the same. The order book settles this, not another order.
            return ProtectionOutcome(
                False,
                "unknown_stop",
                f"The stop for {quantity} of {name} returned no usable answer ({detail}). "
                "It may or may not be resting. Check the order book before doing anything else.",
                quantity,
            )
        logger.warning(
            "live_protection.stop_attempt_failed attempt=%s symbol=%s status=%s detail=%s",
            attempt + 1,
            name,
            status,
            detail,
        )

    # Could not protect it. Closing at market is the lesser harm: a small
    # crystallised loss against an open position with nothing behind it.
    close_status, close_ids, close_detail = await _place_exit(
        session,
        adapter,
        instrument_token=signal.instrument_token,
        side=exit_side,
        quantity=quantity,
        product=product,
        order_type=MARKET,
        trigger_price=Decimal("0"),
        paper_signal_id=signal.id,
        tick=tick,
    )
    if close_status == ACCEPTED:
        return ProtectionOutcome(
            False,
            "flattened",
            f"The stop could not be placed, so the position was closed at market. {close_detail}".strip(),
            quantity,
            close_ids,
            flattened=True,
        )
    return ProtectionOutcome(
        False,
        "exposed",
        f"The stop could not be placed AND the position could not be closed ({close_detail}). "
        f"{quantity} of {name} is open with nothing behind it. Close it by hand now.",
        quantity,
    )
