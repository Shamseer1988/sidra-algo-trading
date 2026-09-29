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
from app.services.broker_adapter import BUY, SELL, BrokerAdapter
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


async def _held_quantity(adapter: BrokerAdapter, symbol: str) -> Decimal | None:
    """The broker's own net quantity for this symbol, or None if unreadable.

    None and zero are kept apart on purpose. Zero means flat, which is safe and
    needs no stop; unreadable means the exposure is unknown, which is the
    opposite, and collapsing them would turn a reason to escalate into a reason
    to do nothing.
    """
    for record in await adapter.normalised_positions():
        if record.symbol != symbol:
            continue
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

    symbol = submission.trading_symbol
    net: Decimal | None = Decimal("0")
    for attempt in range(FILL_POLL_ATTEMPTS):
        net = await _held_quantity(adapter, symbol)
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
        return ProtectionOutcome(True, "flat", "Nothing filled; there is no position to protect.")

    quantity = int(abs(net))
    exit_side = _exit_side(net > 0)
    stop_price = Decimal(str(signal.stop_price))

    for attempt in range(STOP_PLACE_ATTEMPTS):
        status, ids, detail = await _place_exit(
            session,
            adapter,
            instrument_token=signal.instrument_token,
            side=exit_side,
            quantity=quantity,
            product=submission.product,
            order_type=STOP_MARKET,
            trigger_price=stop_price,
            paper_signal_id=signal.id,
        )
        if status == ACCEPTED:
            logger.info(
                "live_protection.stop_placed symbol=%s qty=%s trigger=%s ids=%s", symbol, quantity, stop_price, ids
            )
            return ProtectionOutcome(True, "stopped", f"Stop at {stop_price}.", quantity, ids)
        if status == UNKNOWN:
            # A stop may or may not be resting. Retrying could leave two, and
            # when one triggers the other opens a reversed position; flattening
            # could do the same. The order book settles this, not another order.
            return ProtectionOutcome(
                False,
                "unknown_stop",
                f"The stop for {quantity} of {symbol} returned no usable answer ({detail}). "
                "It may or may not be resting. Check the order book before doing anything else.",
                quantity,
            )
        logger.warning(
            "live_protection.stop_attempt_failed attempt=%s symbol=%s status=%s detail=%s",
            attempt + 1,
            symbol,
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
        product=submission.product,
        order_type=MARKET,
        trigger_price=Decimal("0"),
        paper_signal_id=signal.id,
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
        f"{quantity} of {symbol} is open with nothing behind it. Close it by hand now.",
        quantity,
    )
