"""Resolve a submission whose outcome was never learned.

An UNKNOWN is the one state that cannot be left alone. Until it is resolved
there may be a live order, and therefore live exposure, that no other part of
this system knows about: the risk engine sizes against a portfolio missing it,
reconciliation reports a broker order it cannot explain, and a strategy that
treats UNKNOWN as failure places the order a second time.

Resolution is a lookup, never an inference. The submission was written down with
a ``client_order_id`` before it was sent, and that identifier travels to the
broker in a client-chosen field — ``remarks`` at Firstock, ``tag`` at Upstox.
Recovery reads the order book through the adapter, which knows which, and looks
for it. Nothing in this module names a broker.

**This module never concludes that an order was not placed.** Absence from the
order book is not proof — the book can lag, a field can be renamed, a response
can be truncated — and the cost of being wrong is asymmetric in a way that
decides the design: wrongly concluding "not placed" invites a duplicate live
order, while escalating to a human costs someone a minute. So an attempt that
cannot be found is escalated, not closed.

**One dependency is verified at one broker and not the other.** Upstox documents
``tag`` as a field of every order-book record, so recovery there rests on
documented behaviour. Firstock's reference lists the order book's fields as ones
"such as" orderNumber, status, fillShares, averagePrice, rejectReason and
orderTime — it does not state that ``remarks`` comes back. The same page says to
use the order book to locate orders after an ambiguous place, so some identifier
must survive, but that is inference. If the identifier is absent from every
record, this module reports exactly that rather than falling back to matching on
symbol, side and quantity, which cannot tell our order apart from a second one
like it. Confirm the field with Firstock before relying on live recovery there.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import LiveOrderSubmission
from app.services.broker_adapter import BrokerAdapter, BrokerOrderRecord

logger = logging.getLogger(__name__)

RESOLVED_PLACED = "RESOLVED_PLACED"
NEEDS_REVIEW = "NEEDS_REVIEW"
UNKNOWN = "UNKNOWN"

# After this many failed lookups the attempt stops being a transient ambiguity
# and becomes something a person has to look at.
MAX_RESOLUTION_ATTEMPTS = 3


@dataclass(frozen=True)
class RecoveryResult:
    status: str
    detail: str
    broker_order_numbers: list[str]


def match_submission(order_book: list[BrokerOrderRecord], client_order_id: str) -> RecoveryResult:
    """Find our order in the broker's book by the identifier we chose.

    Pure, so the decision can be tested exhaustively without a broker — which is
    the reason it takes normalised records rather than raw dictionaries. The
    three outcomes are found, definitely-not-findable-this-way, and
    not-found-yet; only the first is a resolution.
    """
    if not client_order_id:
        return RecoveryResult(NEEDS_REVIEW, "Submission has no client order id to search for.", [])

    carries_identifier = False
    numbers: list[str] = []
    for record in order_book:
        if record.client_order_id is not None:
            carries_identifier = True
        if record.client_order_id == client_order_id and record.broker_order_id:
            numbers.append(record.broker_order_id)

    if numbers:
        return RecoveryResult(
            RESOLVED_PLACED,
            f"Found at the broker as {', '.join(numbers)}.",
            numbers,
        )
    if order_book and not carries_identifier:
        # Every safeguard downstream assumes recovery is possible. If it is not,
        # say so plainly instead of degrading into a guess.
        return RecoveryResult(
            NEEDS_REVIEW,
            "The broker's order book did not return the client identifier we sent, so this "
            "submission cannot be identified automatically. Resolve it by hand and confirm "
            "the field with the broker.",
            [],
        )
    return RecoveryResult(UNKNOWN, "Not present in the order book yet.", [])


async def resolve_submission(
    adapter: BrokerAdapter,
    submission: LiveOrderSubmission,
) -> RecoveryResult:
    """Attempt one resolution of one unknown submission.

    Built from a read-only adapter deliberately. Recovery reads; anything that
    could place or cancel an order while resolving an ambiguous one is how a
    single uncertain order becomes two certain ones.
    """
    if submission.status != UNKNOWN:
        return RecoveryResult(submission.status, "Submission is not unknown; nothing to resolve.", [])

    try:
        book = await adapter.normalised_orders()
    except Exception as exc:
        submission.resolution_attempts += 1
        submission.resolution_detail = f"Could not read the order book: {exc}"[:500]
        if submission.resolution_attempts >= MAX_RESOLUTION_ATTEMPTS:
            submission.status = NEEDS_REVIEW
            submission.resolved_at = datetime.now(UTC)
        return RecoveryResult(submission.status, submission.resolution_detail, [])

    result = match_submission(book, submission.client_order_id)
    submission.resolution_attempts += 1
    submission.resolution_detail = result.detail[:500]

    if result.status == RESOLVED_PLACED:
        submission.status = RESOLVED_PLACED
        submission.broker_order_numbers = result.broker_order_numbers
        submission.resolved_at = datetime.now(UTC)
        return result

    if result.status == NEEDS_REVIEW or submission.resolution_attempts >= MAX_RESOLUTION_ATTEMPTS:
        detail = (
            result.detail
            if result.status == NEEDS_REVIEW
            else (
                f"Still absent from the order book after {submission.resolution_attempts} attempts. "
                "This is not proof it was never placed; resolve it by hand before trading this instrument again."
            )
        )
        submission.status = NEEDS_REVIEW
        submission.resolved_at = datetime.now(UTC)
        # Written back, not only returned: the row is what an operator reads, and
        # a row still saying "not present yet" on an escalated submission would
        # tell them to wait when somebody needs to go and look.
        submission.resolution_detail = detail[:500]
        return RecoveryResult(NEEDS_REVIEW, detail, [])
    return result


async def unresolved_submissions(session: AsyncSession) -> list[LiveOrderSubmission]:
    """Every submission whose outcome is still open.

    Both states block: UNKNOWN because it may become exposure, NEEDS_REVIEW
    because a person has been asked to look and has not yet.
    """
    rows = await session.scalars(
        select(LiveOrderSubmission)
        .where(LiveOrderSubmission.status.in_([UNKNOWN, NEEDS_REVIEW, "PREPARED"]))
        .order_by(LiveOrderSubmission.created_at.asc())
    )
    return list(rows.all())
