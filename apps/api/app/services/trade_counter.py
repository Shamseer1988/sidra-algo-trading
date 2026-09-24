"""How many trades the account has actually taken today.

The ceiling used to count **signals**: rows in ``paper_signals`` whose status was
not a risk rejection. That is not what an operator means by "four trades a day".
A signal that passed risk and whose entry order never filled — because the limit
was never touched, or the order was cancelled at the cutoff — spent a quarter of
the day's budget on a trade that did not happen.

The rule here is the one Shamseer specified, and each clause exists because the
old counter got it wrong:

    counted     an ENTRY order that has received at least one fill
    once        further partial fills of that same entry
    zero        a signal, an evaluation, a duplicate signal
    zero        an order that was rejected, or cancelled with nothing filled

**Derived, never accumulated.** The count is a query over what is on disk, so a
restart, a worker crash or a Redis flush cannot lose it and cannot double it.
There is no counter to reconcile because there is no counter — which is the
cheapest way to satisfy "reconcile counts after restart".

**Account-wide.** Every strategy and both brokers share the four. A per-strategy
cap is a different control (``max_trades_per_day``) and is applied separately.

One honest gap, called out rather than hidden. Live fills are not tracked yet:
the live layer records submissions and their broker outcome, not executions. So
a live order that the broker **accepted** counts here, even though the spec says
an unfilled order should count zero. That over-counts, which for a ceiling errs
toward taking fewer trades, and it resolves when live fill tracking arrives in
Phase 5. ``explain()`` says so in words, because an operator who sees 4/4 with
three paper trades deserves to know what the fourth was.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import LiveOrderSubmission, PaperOrder
from app.services.trading_calendar import MARKET_TIMEZONE

# A broker outcome that means an order exists at the exchange. RESOLVED_PLACED
# is an UNKNOWN that recovery later found in the order book, so it is as real as
# an ACCEPTED one; excluding it would let a lost response buy an extra trade.
LIVE_PLACED_STATUSES = ("ACCEPTED", "RESOLVED_PLACED")


def session_bounds_utc(session_date: date) -> tuple[datetime, datetime]:
    """The UTC half-open window covering one IST trading date.

    Computed in Python rather than with a SQL ``AT TIME ZONE`` so the boundary
    is testable without a database and cannot vary with the server's timezone
    configuration — which, for a counter that decides whether a fourth trade is
    allowed, is worth more than the brevity.
    """
    start_ist = datetime.combine(session_date, time.min, tzinfo=MARKET_TIMEZONE)
    return start_ist.astimezone(UTC), (start_ist + timedelta(days=1)).astimezone(UTC)


@dataclass(frozen=True)
class TradeCount:
    """What the day has used, split by where it came from."""

    paper_fills: int
    live_submissions: int

    @property
    def total(self) -> int:
        return self.paper_fills + self.live_submissions

    def remaining(self, ceiling: int) -> int:
        return max(0, ceiling - self.total)

    def explain(self, ceiling: int) -> str:
        parts = []
        if self.paper_fills:
            parts.append(f"{self.paper_fills} paper entr{'y' if self.paper_fills == 1 else 'ies'} filled")
        if self.live_submissions:
            parts.append(
                f"{self.live_submissions} live order{'' if self.live_submissions == 1 else 's'} placed "
                "(live fills are not tracked yet, so an accepted order counts even if it did not fill)"
            )
        detail = "; ".join(parts) if parts else "nothing filled yet"
        return f"{self.total} of {ceiling} used — {detail}."


async def count_filled_entries(session: AsyncSession, session_date: date) -> TradeCount:
    """Today's trades, counted from fills rather than intentions."""
    paper = await session.scalar(
        select(func.count(func.distinct(PaperOrder.id))).where(
            PaperOrder.session_date == session_date,
            PaperOrder.order_role == "ENTRY",
            # Greater than zero, not "is filled": a partially filled entry is a
            # position that exists, and waiting for the remainder before
            # counting it would let a second entry through beside it.
            PaperOrder.filled_quantity > 0,
        )
    )
    start, end = session_bounds_utc(session_date)
    live = await session.scalar(
        select(func.count(LiveOrderSubmission.id)).where(
            LiveOrderSubmission.created_at >= start,
            LiveOrderSubmission.created_at < end,
            LiveOrderSubmission.status.in_(LIVE_PLACED_STATUSES),
        )
    )
    return TradeCount(paper_fills=int(paper or 0), live_submissions=int(live or 0))
