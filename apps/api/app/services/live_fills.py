"""What the broker actually filled, recorded against what we asked for.

An operator compared the P&L calendar with the Upstox app and found them ₹5.34
apart on a ₹170 day -- and on one trade, a ₹0.58 per share loss against the
broker's ₹2.00. Neither number was wrong about what it measured. Rows labelled
LIVE were carrying the *simulator's* fill prices, because
``average_entry_price`` is written in one place only -- the paper execution
engine, filling at completed-candle prices -- and nothing had ever written a
broker fill back. The gap between the two is slippage, and it was invisible
precisely because the screen showed one number where there were two.

This module closes that. Three rules shape it.

**It reads the book reconciliation already reads.** The recorder is handed the
order book rather than fetching one, so recording fills costs nothing against
the rate limit that order placement draws on. There is no second call to make.

**It never corrects the paper position.** The simulator's figures stay exactly
as they were: they are what this system believed at the time, they are what
strategy evaluation is built on, and a record overwritten by a later truth has
nothing left to audit. The broker's figures land in their own columns and the
History screen shows the difference as what it is.

**It does not touch ``status``.** That is this system's lifecycle word, and the
exit manager finds a resting stop by ``status == ACCEPTED``. A stop advanced to
the broker's word would not be found, would not be cancelled, and an exit sent
past a live stop reverses a position rather than closing it. The broker's word
goes in ``broker_status``, which nothing reads to make a decision.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import LiveOrderSubmission
from app.services.broker_adapter import BUY, SELL, BrokerOrderRecord

logger = logging.getLogger(__name__)


def _matches(submission: LiveOrderSubmission, order: BrokerOrderRecord) -> bool:
    """Whether this broker order is one this submission produced.

    Two keys, and both are needed. ``broker_order_numbers`` is a list because a
    broker slices an order above the freeze quantity, so one submission can own
    several rows. ``client_order_id`` is the tag we chose and carried, and it is
    the only key that works for a submission whose broker order number was never
    learned -- which is the case this whole seam exists to recover from.
    """
    numbers = {str(number) for number in (submission.broker_order_numbers or [])}
    if order.broker_order_id and str(order.broker_order_id) in numbers:
        return True
    return bool(order.client_order_id and order.client_order_id == submission.client_order_id)


def record_fills(
    submissions: list[LiveOrderSubmission],
    broker_orders: list[BrokerOrderRecord],
    *,
    now: datetime | None = None,
) -> int:
    """Copy each submission's fill from the order book. Returns rows changed.

    Does not commit; the caller owns the transaction, because this runs inside
    reconciliation and a fill recorded against a reconciliation that then failed
    should roll back with it.

    A broker that reports a filled quantity without a price gives us the
    quantity and nothing else: a price of zero would be read as a fill at zero,
    which on a short is an unbounded profit.
    """
    stamp = now or datetime.now(UTC)
    changed = 0
    for submission in submissions:
        mine = [order for order in broker_orders if _matches(submission, order)]
        if not mine:
            continue

        filled = sum(order.filled_quantity or 0 for order in mine)
        priced = [
            (order.filled_quantity, order.average_price)
            for order in mine
            if order.filled_quantity and order.average_price is not None
        ]
        # Quantity-weighted, because a sliced order fills at several prices and
        # a plain mean would weight a 5-share slice like a 500-share one.
        weighted = (
            sum(quantity * price for quantity, price in priced) / sum(quantity for quantity, _ in priced)
            if priced
            else None
        )
        status = _agreed_status([order.status for order in mine])

        if (
            submission.filled_quantity == filled
            and submission.average_fill_price == weighted
            and submission.broker_status == status
        ):
            continue

        submission.filled_quantity = filled
        if weighted is not None:
            submission.average_fill_price = Decimal(str(weighted)).quantize(Decimal("0.0001"))
        submission.broker_status = status
        submission.fill_seen_at = stamp
        changed += 1
    return changed


def _agreed_status(statuses: list[str]) -> str | None:
    """One status for a submission the broker may have split into several.

    Slices that disagree are reported as the disagreement rather than as
    whichever happened to be first: a submission half filled and half rejected
    is neither COMPLETE nor REJECTED, and saying either would be a claim nobody
    made.
    """
    present = {status for status in statuses if status}
    if not present:
        return None
    if len(present) == 1:
        return present.pop()
    return "PARTIAL"


@dataclass(frozen=True)
class FillSide:
    """One direction of a signal's live trading, as the broker filled it."""

    quantity: int = 0
    price: Decimal | None = None

    @property
    def known(self) -> bool:
        return self.quantity > 0 and self.price is not None


@dataclass(frozen=True)
class SignalFills:
    """Everything the broker filled for one signal, bought and sold.

    Held as buy and sell rather than entry and exit because this module does not
    know which is which -- a long enters by buying and a short by selling, and
    that is the position's business. The money, however, is the same either way.
    """

    buy: FillSide = FillSide()
    sell: FillSide = FillSide()

    def entry(self, side: str) -> FillSide:
        return self.sell if side.upper() == "SHORT" else self.buy

    def exit(self, side: str) -> FillSide:
        return self.buy if side.upper() == "SHORT" else self.sell

    @property
    def matched_quantity(self) -> int:
        """What was both opened and closed. An unclosed remainder is not a result."""
        return min(self.buy.quantity, self.sell.quantity)

    @property
    def gross(self) -> Decimal | None:
        """Sold minus bought, on the quantity that round-tripped.

        The same expression for a long and a short -- a short sells first and
        buys back, which changes the order of events and not the arithmetic.
        None when either side is unfilled or unpriced, so that a half-known
        trade falls back to the modelled figure rather than being reported as a
        total loss of the side that is known.
        """
        if not (self.buy.known and self.sell.known) or self.matched_quantity <= 0:
            return None
        assert self.buy.price is not None and self.sell.price is not None
        return ((self.sell.price - self.buy.price) * self.matched_quantity).quantize(Decimal("0.0001"))


async def fills_by_signal(session: AsyncSession, signal_ids: set[UUID]) -> dict[UUID, SignalFills]:
    """The recorded broker fills for each signal, aggregated by direction.

    Only submissions that actually filled contribute. An order that was placed
    and cancelled -- a protective stop on a trade that reached its target -- is
    part of the story of the trade and no part of its price.
    """
    if not signal_ids:
        return {}

    rows = (
        await session.scalars(
            select(LiveOrderSubmission).where(
                LiveOrderSubmission.paper_signal_id.in_(signal_ids),
                LiveOrderSubmission.filled_quantity.isnot(None),
                LiveOrderSubmission.filled_quantity > 0,
            )
        )
    ).all()

    staged: dict[UUID, dict[str, list[tuple[int, Decimal | None]]]] = {}
    for row in rows:
        side = row.canonical_side
        if side not in (BUY, SELL) or row.paper_signal_id is None:
            continue
        staged.setdefault(row.paper_signal_id, {BUY: [], SELL: []})[side].append(
            (int(row.filled_quantity or 0), row.average_fill_price)
        )

    return {
        signal_id: SignalFills(buy=_aggregate(sides[BUY]), sell=_aggregate(sides[SELL]))
        for signal_id, sides in staged.items()
    }


def _aggregate(parts: list[tuple[int, Decimal | None]]) -> FillSide:
    quantity = sum(part for part, _ in parts)
    priced = [(part, price) for part, price in parts if price is not None]
    if not priced:
        return FillSide(quantity=quantity, price=None)
    weighted = sum(part * price for part, price in priced) / sum(part for part, _ in priced)
    return FillSide(quantity=quantity, price=Decimal(str(weighted)).quantize(Decimal("0.0001")))


async def sweep_session_fills(settings, session: AsyncSession, session_date) -> int:
    """Record every fill for one session, from the broker's order book.

    A safety net for the recorder that runs inside reconciliation, which only
    runs while the deployment is armed and the exchange is open. An activation
    that lapses at two o'clock would otherwise leave the afternoon's fills
    unrecorded, and the trades of that afternoon permanently priced by the
    model -- which is the exact failure this whole seam exists to end.

    Read-only at the broker: the adapter is the report one, whose client has no
    placement method on it at all. Returns the number of submissions changed.
    """
    from app.services.live_execution_gateway import live_report_adapter
    from app.services.trade_counter import session_bounds_utc

    start, end = session_bounds_utc(session_date)
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
    if not submissions:
        return 0

    adapter = await live_report_adapter(settings, session)
    changed = record_fills(submissions, await adapter.normalised_orders())
    if changed:
        await session.commit()
    return changed
