"""Place live orders at whichever broker an operator selected.

Phase 4. This is the first module in the repository that can change state at a
broker, and it is written around one fact: a network call that does not return
is not a call that did not happen.

**The write-ahead rule.** A submission record is created and committed before
the request leaves the process. Writing it afterwards leaves a window in which
an order exists at the exchange and this system has no record of it — and the
consequence is not a missing row, it is a strategy that re-sends the order
because nothing tells it otherwise, or a reconciliation that finds an untracked
position it cannot explain. The window cannot be closed by ordering the code
carefully; it can only be closed by writing first.

**Three outcomes, never two.**

    ACCEPTED   the broker returned order numbers
    REJECTED   the broker refused, and said why
    UNKNOWN    we never learned which of the above happened

UNKNOWN is the outcome that matters. Collapsing it into REJECTED produces
duplicate live orders; collapsing it into ACCEPTED produces phantom positions
the risk engine sizes against. So an UNKNOWN is never retried here and never
guessed at: it is recorded, and resolved by looking the order up in the broker's
own book through ``live_order_recovery``.

**Identity.** ``client_order_id`` travels to the broker in a client-chosen
field — ``remarks`` at Firstock, ``tag`` at Upstox — and the adapter knows which.
That is what makes recovery possible at all: an attempt whose outcome was never
learned can be found by the identifier we chose, instead of being guessed at from
symbol, side and quantity, which cannot distinguish our order from a second one
like it.

**Slicing.** Both brokers can split an order that exceeds the exchange freeze
quantity, so one submission can produce several order ids. They are all stored; a
schema that held one would silently lose the remainder, and the lost slices are
real exposure.

**Two vocabularies, written down together.** The request is canonical — BUY,
LIMIT, INTRADAY, an instrument token — and the record also stores what the broker
was actually asked for, resolved by the adapter *before* the row is written. An
operator resolving an UNKNOWN is then reading the same words the broker's order
book shows them, rather than translating in their head against a live position.

Nothing in this module decides whether an order *should* be sent. That is
``live_execution``'s job, and it is kept separate so that the code which can
place an order contains no conditions that might be edited into permissiveness.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import LiveOrderSubmission
from app.services.broker_adapter import (
    BrokerAdapter,
    BrokerOrder,
    BrokerOrderDescription,
)

logger = logging.getLogger(__name__)

ACCEPTED = "ACCEPTED"
REJECTED = "REJECTED"
UNKNOWN = "UNKNOWN"

PREPARED = "PREPARED"

# The broker's client-tag field carries our identifier. Keep it short enough for
# Upstox's 40-character tag limit and long enough to be unique without
# coordination.
CLIENT_ORDER_ID_PREFIX = "sidra"


@dataclass(frozen=True)
class LiveOrderRequest:
    """Everything the broker needs, and nothing that decides whether to send it.

    Stated in the canonical vocabulary rather than any broker's, so that the
    gates above it, the approval message an operator reads, and the audit trail
    all say the same thing regardless of where the order ends up going.
    """

    instrument_token: str
    side: str
    quantity: int
    order_type: str
    product: str
    price: Decimal
    trigger_price: Decimal = Decimal("0")
    validity: str = "DAY"

    def to_broker_order(self, client_order_id: str) -> BrokerOrder:
        return BrokerOrder(
            instrument_token=self.instrument_token,
            side=self.side,
            quantity=self.quantity,
            order_type=self.order_type,
            product=self.product,
            price=self.price,
            client_order_id=client_order_id,
            trigger_price=self.trigger_price,
            validity=self.validity,
        )


@dataclass(frozen=True)
class SubmissionOutcome:
    status: str
    broker_order_numbers: list[str] = field(default_factory=list)
    detail: str = ""
    failure_code: str | None = None
    failure_name: str | None = None

    @property
    def is_unknown(self) -> bool:
        return self.status == UNKNOWN


def new_client_order_id() -> str:
    """A short, unique identifier for one submission attempt."""
    return f"{CLIENT_ORDER_ID_PREFIX}-{uuid.uuid4().hex[:16]}"


def _order_numbers(data: Any) -> list[str]:
    """Collect every order number the broker returned.

    Accepts the single-order shape and the sliced shape, because one submission
    can become several broker orders and losing the extras loses real exposure.
    """
    numbers: list[str] = []

    def _collect(value: Any) -> None:
        if isinstance(value, dict):
            number = value.get("orderNumber")
            if number is not None and str(number).strip():
                numbers.append(str(number).strip())
            for nested in value.values():
                if isinstance(nested, list | dict):
                    _collect(nested)
        elif isinstance(value, list):
            for item in value:
                _collect(item)

    _collect(data)
    # Preserve order, drop duplicates: the same number nested twice is one order.
    seen: set[str] = set()
    unique: list[str] = []
    for number in numbers:
        if number not in seen:
            seen.add(number)
            unique.append(number)
    return unique


async def prepare_submission(
    session: AsyncSession,
    request: LiveOrderRequest,
    description: BrokerOrderDescription,
    *,
    broker: str,
    client_order_id: str,
    paper_signal_id: Any = None,
    oms_order_id: Any = None,
    approval_reference: str | None = None,
) -> LiveOrderSubmission:
    """Write the intent down. The caller must commit before sending.

    Split from ``send_prepared_order`` so that the commit boundary is the
    caller's and therefore visible. A single function that did both would make
    the write-ahead guarantee depend on a transaction the reader cannot see.

    Takes the resolved ``description`` rather than resolving it here, so the row
    records the order in the broker's own words — and so a symbol that cannot be
    named is refused before anything is written, not discovered mid-send.
    """
    record = LiveOrderSubmission(
        client_order_id=client_order_id,
        paper_signal_id=paper_signal_id,
        oms_order_id=oms_order_id,
        approval_reference=approval_reference,
        broker=broker,
        exchange=description.exchange,
        trading_symbol=description.symbol,
        product=description.product,
        price_type=description.order_type,
        transaction_type=description.side,
        retention=description.validity,
        quantity=request.quantity,
        price=request.price,
        trigger_price=request.trigger_price,
        status=PREPARED,
        request_snapshot={
            "broker": broker,
            # What the broker is being asked for.
            "exchange": description.exchange,
            "symbol": description.symbol,
            "product": description.product,
            "orderType": description.order_type,
            "side": description.side,
            "validity": description.validity,
            "quantity": str(request.quantity),
            "price": str(request.price),
            "triggerPrice": str(request.trigger_price),
            "clientOrderId": client_order_id,
            # What this system decided, before translation. Kept alongside so a
            # mapping bug is visible in the record rather than only in its
            # consequences.
            "canonical": {
                "instrumentToken": request.instrument_token,
                "side": request.side,
                "orderType": request.order_type,
                "product": request.product,
                "validity": request.validity,
            },
        },
    )
    session.add(record)
    await session.flush()
    return record


async def send_prepared_order(
    adapter: BrokerAdapter,
    record: LiveOrderSubmission,
    request: LiveOrderRequest,
    description: BrokerOrderDescription,
) -> SubmissionOutcome:
    """Send one prepared order and classify the result. Never retries.

    A retry here would be a second order, because the failure this function can
    encounter most often — a timeout — is exactly the case where the first one
    may already be live. Retrying is the caller's decision to make with the
    order book in hand, not this function's to make blind.

    The classification is the adapter's; this function only carries it across.
    Re-deriving it here would mean two places that decide what UNKNOWN means,
    and they would drift.
    """
    submission = await adapter.submit(request.to_broker_order(record.client_order_id), description)
    return SubmissionOutcome(
        status=submission.status,
        broker_order_numbers=list(submission.broker_order_ids),
        detail=submission.detail,
        failure_code=submission.failure_code,
        failure_name=submission.failure_name,
    )


def apply_outcome(record: LiveOrderSubmission, outcome: SubmissionOutcome, response: Any = None) -> None:
    """Record what happened. The caller commits."""
    record.status = outcome.status
    record.broker_order_numbers = list(outcome.broker_order_numbers)
    record.failure_code = outcome.failure_code
    record.failure_name = outcome.failure_name
    record.failure_message = outcome.detail[:500] if outcome.detail else None
    record.sent_at = datetime.now(UTC)
    if isinstance(response, dict):
        record.response_snapshot = response
    if outcome.status in {ACCEPTED, REJECTED}:
        record.resolved_at = datetime.now(UTC)
