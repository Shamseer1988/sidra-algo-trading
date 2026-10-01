"""Compare the broker's view of the account against our own.

Phase 2 of the live-execution layer. Nothing here submits, modifies or cancels:
it reads broker state through a read-only adapter and decides one thing — whether
our record of the world matches the broker's well enough to trade.

Which broker is not this module's business. It works from the normalised records
the adapter produces, so the same logic — and the same tests — cover Upstox and
Firstock, rather than two nearly-identical parsers that would drift.

The default answer is no. ``safe_to_trade`` is granted only when every check
passes, so a bug that skips a check, an exception mid-way, or an endpoint that
returns an unexpected shape all fail closed rather than open.

Why blocking is the right default for each finding:

``UNTRACKED_BROKER_ORDER``
    The broker has an order we have no record of. Either something else is
    trading this account, or we lost a submission response. Blocking while it is
    still working, because adding orders on top of an exposure we cannot explain
    is how a small problem becomes a large one.

    Review, not blocking, once it has finished. The reason to block is unexplained
    exposure, and a finished order's exposure is already in the position book,
    where ``UNEXPLAINED_POSITION`` reads it directly and blocks on it. Blocking
    here too checks the same fact by a proxy that never clears -- the order book
    keeps the row all day -- so a single manual square-off ended live trading
    until the next session. It stays visible, and the exposure check remains the
    thing that stops trading.

``UNEXPLAINED_POSITION``
    Net quantity at the broker with no local position to account for it. Same
    reasoning, and worse: the risk engine would size new trades against a
    portfolio it does not know the shape of.

``UNKNOWN_SUBMISSION``
    One of our own orders is in UNKNOWN — we never learned whether it reached the
    exchange. Resolving it needs the Order Book, not another order.

``STATUS_DIVERGENCE``
    We believe an order is finished and the broker says it is still working, or
    the reverse. Our stop-loss accounting is wrong in one direction or the other.

``UNREADABLE_ORDER_STATUS``
    The broker reported a status no adapter recognises. It is blocking for the
    same reason an unreadable position is: we cannot tell whether the order is
    working or finished, and an unrecognised status that fell into the gap
    between the two would be silently ignored by every check below.

``MISSING_AT_BROKER`` is the one finding that only warrants review: an order we
created but have not submitted looks exactly like this, and that is a normal
state between intent and submission.
"""

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ExecutionReconciliation, LiveOrderSubmission, OmsOrder
from app.services.broker_adapter import (
    OPEN_STATUSES,
    STATUS_UNREADABLE,
    TERMINAL_STATUSES,
    BrokerAdapter,
    BrokerPositionRecord,
)
from app.services.trade_counter import LIVE_PLACED_STATUSES, session_bounds_utc
from app.services.trading_calendar import MARKET_TIMEZONE

# Our own terminal states, from services/oms.py.
OMS_TERMINAL_STATUSES = frozenset({"FILLED", "CANCELLED", "REJECTED"})

BLOCKING = "BLOCKING"
REVIEW = "REVIEW"


@dataclass(frozen=True)
class LocalOrder:
    """One of our records of an order, whichever table it came from.

    Two tables hold orders and they mean different things. ``OmsOrder`` models a
    lifecycle and reaches FILLED or CANCELLED; ``LiveOrderSubmission`` records a
    single submission attempt and is never updated again, because nothing polls
    fills. Reading them as the same thing is how the first live order this
    system placed came back as UNTRACKED_BROKER_ORDER: the reconciler looked
    only at OmsOrder, and live submission writes the other table.

    ``tracks_lifecycle`` keeps them apart where it matters. Applying the status
    divergence checks to a submission would report every filled order as a
    disagreement, because our row says ACCEPTED for ever and the broker's says
    COMPLETE — which is not a divergence, it is the order working.
    """

    identifier: str
    status: str
    tracks_lifecycle: bool


@dataclass(frozen=True)
class Finding:
    kind: str
    severity: str
    detail: str
    broker_order_number: str | None = None
    oms_order_id: str | None = None


@dataclass(frozen=True)
class LiveReconciliationReport:
    status: str
    safe_to_trade: bool
    internal_orders: int
    external_orders: int
    unknown_orders: int
    checked_at: datetime
    findings: list[Finding] = field(default_factory=list)

    @property
    def blocking(self) -> list[Finding]:
        return [item for item in self.findings if item.severity == BLOCKING]

    def summary(self) -> str:
        """One line for the 255-character detail column.

        The review count is stated even when trading is safe. It did not need to
        be while every untracked order blocked -- safe_to_trade and "no findings"
        meant the same thing. Once a *finished* untracked order became review
        rather than blocking, they stopped meaning the same thing, and this line
        went on saying "broker and local state agree" beside a status of
        REQUIRES_REVIEW. A record that contradicts itself is worse than one that
        says nothing: the operator has to decide which half to believe.
        """
        review = len(self.findings) - len(self.blocking)
        if self.safe_to_trade:
            agree = f"Broker and local state agree: {self.internal_orders} local, {self.external_orders} broker orders."
            if review:
                return f"{agree} {review} noted for review, none blocking."[:255]
            return agree
        blocking = len(self.blocking)
        review = len(self.findings) - blocking
        kinds = ", ".join(sorted({item.kind for item in self.blocking})) or "none"
        return f"Trading blocked: {blocking} blocking ({kinds}), {review} for review."[:255]


def _plausible_range(submissions: list[LiveOrderSubmission], position: BrokerPositionRecord) -> tuple[Decimal, Decimal]:
    """The most long and the most short our own orders today could have left.

    Counted from submissions the broker accepted, because a rejected one created
    nothing. Gross rather than net on each side so a partially filled exit still
    falls inside the range: we sent a buy for 143 and a sell for 143, so any net
    from -143 to +143 is ours, whatever filled.

    Matched on the instrument token. This used to compare the position's symbol
    against the submission's ``trading_symbol``, which on Upstox are
    "BHARTIARTL" and "NSE_EQ|INE397D01024" -- never equal, so the range came
    back (0, 0) and a position this system had just opened itself was reported
    as UNEXPLAINED_POSITION and blocked all further trading. The gate was right
    to block on what it was told; it was being told the wrong thing.
    """
    gross_long = Decimal("0")
    gross_short = Decimal("0")
    for submission in submissions:
        if submission.status not in LIVE_PLACED_STATUSES:
            continue
        if not position.identifies(submission.instrument_token, submission.trading_symbol):
            continue
        quantity = Decimal(str(submission.quantity or 0))
        if submission.transaction_type == "BUY":
            gross_long += quantity
        elif submission.transaction_type == "SELL":
            gross_short += quantity
    return gross_long, gross_short


async def reconcile_live_execution(
    session: AsyncSession,
    adapter: BrokerAdapter,
) -> LiveReconciliationReport:
    """Read broker state and decide whether it is safe to trade.

    Raises nothing on a broker failure: an unreachable broker produces a blocked
    report rather than an exception, because "we could not check" and "we checked
    and it was wrong" must both stop trading, and only one of them is an error
    the caller can do anything about.
    """
    checked_at = datetime.now(UTC)
    findings: list[Finding] = []

    try:
        broker_orders = await adapter.normalised_orders()
        broker_positions = await adapter.normalised_positions()
    except Exception as exc:
        # Deliberately fail closed. We cannot prove the account is in a safe state.
        return LiveReconciliationReport(
            status="BLOCKED",
            safe_to_trade=False,
            internal_orders=0,
            external_orders=0,
            unknown_orders=0,
            checked_at=checked_at,
            findings=[
                Finding(
                    kind="BROKER_UNREACHABLE",
                    severity=BLOCKING,
                    detail=f"Could not read broker state: {exc}",
                )
            ],
        )

    oms_orders = list((await session.scalars(select(OmsOrder))).all())
    by_broker_id: dict[str, LocalOrder] = {
        order.broker_order_id: LocalOrder(str(order.id), order.status, tracks_lifecycle=True)
        for order in oms_orders
        if order.broker_order_id
    }

    # Scoped to today because the broker's order book is. An accepted submission
    # from last week is not missing from a book that never claimed to hold it,
    # and unscoped rows would accumulate into a permanent wall of findings.
    start, end = session_bounds_utc(checked_at.astimezone(MARKET_TIMEZONE).date())
    submissions = list(
        (
            await session.scalars(
                select(LiveOrderSubmission).where(
                    LiveOrderSubmission.created_at >= start,
                    LiveOrderSubmission.created_at < end,
                )
            )
        ).all()
    )
    for submission in submissions:
        for number in submission.broker_order_numbers or []:
            # An OmsOrder wins: it is the richer record, and only one of the two
            # can answer a question about the order's lifecycle.
            by_broker_id.setdefault(
                str(number), LocalOrder(str(submission.id), submission.status, tracks_lifecycle=False)
            )

    unknown_orders = 0
    for order in oms_orders:
        if order.status != "UNKNOWN":
            continue
        unknown_orders += 1
        findings.append(
            Finding(
                kind="UNKNOWN_SUBMISSION",
                severity=BLOCKING,
                detail="Submission outcome was never established; resolve from the order book before trading.",
                oms_order_id=str(order.id),
                broker_order_number=order.broker_order_id,
            )
        )
    for submission in submissions:
        if submission.status != "UNKNOWN":
            continue
        unknown_orders += 1
        findings.append(
            Finding(
                kind="UNKNOWN_SUBMISSION",
                severity=BLOCKING,
                detail=(
                    f"Live submission {submission.client_order_id} never learned its outcome; "
                    "find it in the order book by that tag before trading."
                ),
                oms_order_id=str(submission.id),
            )
        )

    seen_broker_ids: set[str] = set()
    for record in broker_orders:
        number = record.broker_order_id
        if not number:
            continue
        seen_broker_ids.add(number)
        status = record.status
        local = by_broker_id.get(number)

        if status == STATUS_UNREADABLE:
            findings.append(
                Finding(
                    kind="UNREADABLE_ORDER_STATUS",
                    severity=BLOCKING,
                    detail=(
                        f"Broker order {number} reported status "
                        f"{record.raw.get('status')!r}, which no status map recognises. "
                        "Whether it is still working cannot be established."
                    ),
                    broker_order_number=number,
                    oms_order_id=local.identifier if local is not None else None,
                )
            )
            continue

        if local is None:
            # An order that has already finished is not the same risk as one
            # still working, and treating them alike made this gate impossible
            # to clear. A working order we have no record of is exposure about
            # to arrive that nothing here can account for: still blocking. A
            # finished one has already had its effect, and that effect is
            # exposure, which the position check below reads from the broker's
            # own book and blocks on independently. Blocking here as well
            # checks the same fact twice -- once directly, where it clears when
            # the account is flat, and once by proxy, where it never clears at
            # all, because the order book keeps the row for the rest of the day.
            #
            # The case that forced this: the operator squared off an unprotected
            # position by hand, exactly as this system's own alert told them to,
            # and the resulting COMPLETE order blocked all trading until the
            # next session. A safety gate that punishes following its own
            # instruction gets worked around, and a gate that is worked around
            # protects nothing.
            #
            # Anything neither open nor terminal still blocks: an unrecognised
            # status is not evidence that an order has finished.
            settled = status in TERMINAL_STATUSES
            findings.append(
                Finding(
                    kind="UNTRACKED_BROKER_ORDER",
                    severity=REVIEW if settled else BLOCKING,
                    detail=(
                        f"Broker order {number} ({status}) has no local record. It has finished, so any "
                        "position it left is checked against the broker's position book rather than here."
                        if settled
                        else f"Broker order {number} ({status or 'unknown status'}) has no local record "
                        "and may still fill. Exposure it would create cannot be accounted for."
                    ),
                    broker_order_number=number,
                )
            )
            continue

        if not local.tracks_lifecycle:
            # We know we sent it and the broker owns it from here. Our row does
            # not move, so comparing the two statuses would manufacture a
            # disagreement out of an order simply working.
            continue

        local_terminal = local.status in OMS_TERMINAL_STATUSES
        broker_working = status in OPEN_STATUSES
        if local_terminal and broker_working:
            findings.append(
                Finding(
                    kind="STATUS_DIVERGENCE",
                    severity=BLOCKING,
                    detail=f"Local state is {local.status} but the broker still shows {status}.",
                    broker_order_number=number,
                    oms_order_id=local.identifier,
                )
            )
        elif not local_terminal and status in TERMINAL_STATUSES:
            findings.append(
                Finding(
                    kind="STATUS_DIVERGENCE",
                    severity=BLOCKING,
                    detail=f"Broker reports {status} but local state is still {local.status}.",
                    broker_order_number=number,
                    oms_order_id=local.identifier,
                )
            )

    for number, order in by_broker_id.items():
        if number in seen_broker_ids or order.status in OMS_TERMINAL_STATUSES:
            continue
        findings.append(
            Finding(
                kind="MISSING_AT_BROKER",
                severity=REVIEW,
                detail=f"Local order {order.status} references broker order {number}, absent from the order book.",
                broker_order_number=number,
                oms_order_id=order.identifier,
            )
        )

    for position in broker_positions:
        symbol = position.symbol
        net = position.net_quantity
        if net is None:
            findings.append(
                Finding(
                    kind="UNREADABLE_POSITION",
                    severity=BLOCKING,
                    detail=(
                        f"Broker reported an unreadable net quantity for {symbol}. "
                        "Exposure cannot be established, so trading must not continue."
                    ),
                )
            )
            continue
        if net == 0:
            continue

        # What our own orders could have produced, from the submissions we know
        # we placed today. A net inside that range is exposure this system
        # created; outside it, something else did, and that is the case the
        # finding was always meant to catch.
        gross_long, gross_short = _plausible_range(submissions, position)
        if net == gross_long - gross_short:
            continue
        if -gross_short <= net <= gross_long:
            findings.append(
                Finding(
                    kind="PARTIAL_POSITION",
                    severity=REVIEW,
                    detail=(
                        f"Broker holds net {net} in {symbol}; our orders today net to "
                        f"{gross_long - gross_short}. Consistent with a partial fill."
                    ),
                )
            )
            continue
        findings.append(
            Finding(
                kind="UNEXPLAINED_POSITION",
                severity=BLOCKING,
                detail=(
                    f"Broker holds net {net} in {symbol}, outside anything our orders today could "
                    f"have produced (at most {gross_long} long, {gross_short} short). "
                    "Review before trading."
                ),
            )
        )

    blocking = [item for item in findings if item.severity == BLOCKING]
    status = "CLEAN" if not findings else ("BLOCKED" if blocking else "REQUIRES_REVIEW")
    return LiveReconciliationReport(
        status=status,
        safe_to_trade=not blocking,
        internal_orders=len(oms_orders) + len(submissions),
        external_orders=len(broker_orders),
        unknown_orders=unknown_orders,
        checked_at=checked_at,
        findings=findings,
    )


async def persist_live_reconciliation(
    session: AsyncSession,
    report: LiveReconciliationReport,
) -> ExecutionReconciliation:
    """Record the verdict and its evidence.

    Every live reconciliation is persisted, including clean ones: the absence of a
    recent record is itself a reason to refuse to trade, and that check only works
    if success is written down too.
    """
    record = ExecutionReconciliation(
        mode="LIVE",
        status=report.status,
        internal_orders=report.internal_orders,
        external_orders=report.external_orders,
        unknown_orders=report.unknown_orders,
        detail=report.summary(),
        findings=[asdict(item) for item in report.findings],
        safe_to_trade=report.safe_to_trade,
    )
    session.add(record)
    await session.flush()
    return record
