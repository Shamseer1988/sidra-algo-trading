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
    The broker has a live order we have no record of. Either something else is
    trading this account, or we lost a submission response. Adding orders on top
    of an exposure we cannot explain is how a small problem becomes a large one.

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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ExecutionReconciliation, OmsOrder
from app.services.broker_adapter import (
    OPEN_STATUSES,
    STATUS_UNREADABLE,
    TERMINAL_STATUSES,
    BrokerAdapter,
)

# Our own terminal states, from services/oms.py.
OMS_TERMINAL_STATUSES = frozenset({"FILLED", "CANCELLED", "REJECTED"})

BLOCKING = "BLOCKING"
REVIEW = "REVIEW"


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
        """One line for the 255-character detail column."""
        if self.safe_to_trade:
            return f"Broker and local state agree: {self.internal_orders} local, {self.external_orders} broker orders."
        blocking = len(self.blocking)
        review = len(self.findings) - blocking
        kinds = ", ".join(sorted({item.kind for item in self.blocking})) or "none"
        return f"Trading blocked: {blocking} blocking ({kinds}), {review} for review."[:255]


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
    by_broker_id = {order.broker_order_id: order for order in oms_orders if order.broker_order_id}

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
                    oms_order_id=str(local.id) if local is not None else None,
                )
            )
            continue

        if local is None:
            findings.append(
                Finding(
                    kind="UNTRACKED_BROKER_ORDER",
                    severity=BLOCKING,
                    detail=f"Broker order {number} ({status or 'unknown status'}) has no local record.",
                    broker_order_number=number,
                )
            )
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
                    oms_order_id=str(local.id),
                )
            )
        elif not local_terminal and status in TERMINAL_STATUSES:
            findings.append(
                Finding(
                    kind="STATUS_DIVERGENCE",
                    severity=BLOCKING,
                    detail=f"Broker reports {status} but local state is still {local.status}.",
                    broker_order_number=number,
                    oms_order_id=str(local.id),
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
                oms_order_id=str(order.id),
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
        findings.append(
            Finding(
                kind="UNEXPLAINED_POSITION",
                severity=BLOCKING,
                detail=(
                    f"Broker holds net {net} in {symbol}. Live position tracking is not implemented, "
                    "so any open exposure is unexplained and must be reviewed before trading."
                ),
            )
        )

    blocking = [item for item in findings if item.severity == BLOCKING]
    status = "CLEAN" if not findings else ("BLOCKED" if blocking else "REQUIRES_REVIEW")
    return LiveReconciliationReport(
        status=status,
        safe_to_trade=not blocking,
        internal_orders=len(oms_orders),
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
