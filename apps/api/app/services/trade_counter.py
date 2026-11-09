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

**Account-wide, and counted once.** Every strategy and both brokers share the
four. A per-strategy cap is a different control (``max_trades_per_day``) and is
applied separately.

Once, because a live deployment records the same trade twice: the scanner
queues a paper entry for every signal it accepts, and the live path submits that
same signal to the broker. Summing the two tables charged one round trip to the
budget twice, so with a ceiling of two the first live trade of the morning ended
the day and no further signal could be taken. The paper row is a journal of the
live trade, not a second trade, and the money is only at risk once.

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

from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import LiveOrderSubmission, PaperOrder, PaperSignal
from app.services.trading_calendar import MARKET_TIMEZONE

# A broker outcome that means an order exists at the exchange. RESOLVED_PLACED
# is an UNKNOWN that recovery later found in the order book, so it is as real as
# an ACCEPTED one; excluding it would let a lost response buy an extra trade.
#
# Used for money: P&L, reconciliation, execution quality, the broker's own day
# figures. Those are questions about trades that really happened, and an order
# still in flight has not happened.
LIVE_PLACED_STATUSES = ("ACCEPTED", "RESOLVED_PLACED")

# An order that MAY exist at the exchange, which for a ceiling is the same thing
# as one that does.
#
# PREPARED is written and committed *before* the request leaves the process, on
# purpose -- an order at the exchange that this system has no record of is the
# worse failure. So between that commit and the broker's answer there is a row
# for a real order, and reading only LIVE_PLACED_STATUSES made every daily gate
# blind to it for the length of a round trip. On 9 November four entries were
# sent in the same second, 09:37:01, and all three daily gates passed all four:
# the trade ceiling of 3 took 4, the risk budget of 375 took 500, and the
# two-position limit took four positions held together for two and a half
# hours. Three of the four were banks.
#
# UNKNOWN is a lost response: the order may be live and we were not told.
# NEEDS_REVIEW is recovery giving up on an UNKNOWN, and its own resolution text
# says "this is not proof it was never placed; resolve it by hand before
# trading this instrument again" -- which a ceiling must take at its word.
#
# REJECTED is the only outcome that frees what it reserved, because the broker
# said no and there is nothing at the exchange.
LIVE_IN_FLIGHT_STATUSES = ("PREPARED", "UNKNOWN", "NEEDS_REVIEW")

# What a *ceiling* counts: everything the account may be on the hook for. Used
# by the daily trade count here and by the daily risk allocation in live_risk.
# Erring this way costs at most a trade not taken; erring the other way cost
# 2.0% of capital on a day configured for 1.5%.
LIVE_COMMITTED_STATUSES = LIVE_PLACED_STATUSES + LIVE_IN_FLIGHT_STATUSES


def session_bounds_utc(session_date: date) -> tuple[datetime, datetime]:
    """The UTC half-open window covering one IST trading date.

    Computed in Python rather than with a SQL ``AT TIME ZONE`` so the boundary
    is testable without a database and cannot vary with the server's timezone
    configuration — which, for a counter that decides whether a fourth trade is
    allowed, is worth more than the brevity.
    """
    start_ist = datetime.combine(session_date, time.min, tzinfo=MARKET_TIMEZONE)
    return start_ist.astimezone(UTC), (start_ist + timedelta(days=1)).astimezone(UTC)


async def first_live_order_at(session: AsyncSession, session_date: date) -> datetime | None:
    """When the first live order of a session reached the broker, or None.

    Exists so a report can tell an overnight settings edit from one made while
    orders were live. A limit saved at 02:30 cannot have affected any order of
    a day whose first entry is impossible before 09:30, and a report that
    flagged every such edit would cry wolf on every run and be ignored by the
    third one.
    """
    start, end = session_bounds_utc(session_date)
    return await session.scalar(
        select(func.min(LiveOrderSubmission.created_at)).where(
            LiveOrderSubmission.created_at >= start,
            LiveOrderSubmission.created_at < end,
            LiveOrderSubmission.status.in_(LIVE_PLACED_STATUSES),
        )
    )


def current_session_date() -> date:
    """Today's trading date in IST, which is not today's UTC date all day.

    IST is UTC+5:30, so between midnight and 05:30 IST the UTC calendar still
    reads yesterday. Market hours are 09:15-15:30 IST (03:45-10:00 UTC) and the
    two agree throughout, which is why reading the UTC date never broke a live
    gate -- and why it survived unnoticed: the report simply spoke about the
    wrong day to anyone looking after midnight, and a halt recorded in those
    five hours would have filed under the session before it.

    The rest of this codebase derives the session date in IST. These helpers
    exist so the live risk engine does too, rather than agreeing by accident.
    """
    return datetime.now(MARKET_TIMEZONE).date()


@dataclass(frozen=True)
class TradeCount:
    """What the day has used, split by where it came from.

    ``both`` is the number of signals that appear in each side. On a live
    deployment that is normally every live trade: the scanner queues a paper
    entry for every signal it accepts and the live path submits the same signal
    to the broker, so one trade lands in both tables. Adding the two counts made
    a single round trip spend two of the day's trades, and with a ceiling of two
    the first live trade of the morning ended the day.
    """

    paper_fills: int
    live_submissions: int
    both: int = 0

    @property
    def total(self) -> int:
        """Distinct trades. A signal traded on paper and live is one trade."""
        return self.paper_fills + self.live_submissions - self.both

    def remaining(self, ceiling: int) -> int:
        return max(0, ceiling - self.total)

    def explain(self, ceiling: int) -> str:
        parts = []
        if self.paper_fills:
            parts.append(f"{self.paper_fills} paper entr{'y' if self.paper_fills == 1 else 'ies'} filled")
        if self.live_submissions:
            parts.append(
                f"{self.live_submissions} live trade{'' if self.live_submissions == 1 else 's'} placed "
                "(counted per signal, so an entry and the orders that close it are one trade; "
                "live fills are not tracked yet, so an accepted entry counts even if it did not fill)"
            )
        if self.both:
            parts.append(
                f"{self.both} of those {'is the same trade' if self.both == 1 else 'are the same trades'} "
                "on both sides, counted once"
            )
        detail = "; ".join(parts) if parts else "nothing filled yet"
        return f"{self.total} of {ceiling} used — {detail}."


async def count_filled_entries(
    session: AsyncSession,
    session_date: date,
    *,
    strategy_type: str | None = None,
    side: str | None = None,
    instrument_token: str | None = None,
) -> TradeCount:
    """Today's trades, counted from fills rather than intentions.

    The keys are read rather than counted, because the two tables overlap and
    only the keys can say by how much. On a live deployment the scanner queues a
    paper entry for every signal it accepts *and* the live path submits the same
    signal, so summing two counts charged one trade to the budget twice.

    ``strategy_type`` and ``side`` narrow it to one strategy's own budget. The
    per-strategy caps used to count accepted *evaluations*, which is the thing
    this function exists not to do: an evaluation is written before the risk
    engine is asked, so a signal refused for "Maximum concurrent paper
    positions reached" spent one of the strategy's trades without ever becoming
    one. With two positions allowed that refusal is routine -- it is what
    happens whenever both slots are full -- so the strategy's budget drained
    fastest on the days it traded least, which is exactly backwards.

    Narrowed by strategy *type* rather than by the configuration's id, because
    a signal does not carry the id: it stores ``"{type}@{version}"``, and the
    version moves whenever the operator saves a setting. Matching the whole
    string would restart the day's count mid-session on an afternoon edit --
    13 October's settings change took this strategy from v12 to v15. The type
    alone is stable, and it separates the four strategies here because each is
    a different type. Two enabled configurations of the *same* type would share
    a budget under this count; nothing in the system creates that today, and a
    signal would have to carry its configuration's id for it to be fixable.

    ``instrument_token`` narrows it to one share, for the per-share ceiling.
    Across every strategy, because the thing that ceiling is about is the
    company: on 6 November two different strategies entered JSWSTEEL short in
    the same minute, each inside its own per-strategy cap, and the account held
    two trades' worth of risk on one company's news. Neither strategy knew
    what the other had taken; this count does.

    A live order with no signal behind it cannot be attributed to a strategy,
    so it is left out of a narrowed count. It still reaches the account-wide
    ceiling, where an order resting at the exchange belongs however it got
    there. The same is true of the per-share count: a signal-less order cannot
    be attributed to a share either.
    """
    narrowed = strategy_type is not None or side is not None or instrument_token is not None
    signal_ids: set[str] | None = None
    if narrowed:
        conditions = [PaperSignal.session_date == session_date]
        if strategy_type is not None:
            conditions.append(PaperSignal.strategy_version.startswith(f"{strategy_type}@"))
        if side is not None:
            conditions.append(PaperSignal.side == side)
        if instrument_token is not None:
            conditions.append(PaperSignal.instrument_token == instrument_token)
        rows = await session.scalars(select(PaperSignal.id).where(*conditions))
        signal_ids = {str(value) for value in rows.all()}
        if not signal_ids:
            return TradeCount(paper_fills=0, live_submissions=0, both=0)

    paper_rows = await session.scalars(
        select(PaperOrder.paper_signal_id).where(
            PaperOrder.session_date == session_date,
            PaperOrder.order_role == "ENTRY",
            # Greater than zero, not "is filled": a partially filled entry is a
            # position that exists, and waiting for the remainder before
            # counting it would let a second entry through beside it.
            PaperOrder.filled_quantity > 0,
        )
    )
    paper_keys = {str(value) for value in paper_rows if value is not None}
    if signal_ids is not None:
        paper_keys &= signal_ids

    start, end = session_bounds_utc(session_date)
    # Distinct signals, not rows. One trade now writes three submissions -- the
    # entry, the protective stop behind it, and the exit that closes it -- and
    # counting rows made a single round trip consume three of the day's four
    # trades. They share a signal, so the signal is the trade.
    # Falling back to the row id rather than dropping signal-less rows: an order
    # placed outside a signal still exists at the exchange, and excluding it
    # would let one escape the day's ceiling entirely.
    trade_key = func.coalesce(cast(LiveOrderSubmission.paper_signal_id, String), cast(LiveOrderSubmission.id, String))
    live_rows = await session.scalars(
        select(trade_key)
        .where(
            LiveOrderSubmission.created_at >= start,
            LiveOrderSubmission.created_at < end,
            # Committed, not placed: see LIVE_IN_FLIGHT_STATUSES. A ceiling that
            # waits for the broker's answer is not a ceiling for the length of a
            # round trip, which is exactly long enough to send another order.
            LiveOrderSubmission.status.in_(LIVE_COMMITTED_STATUSES),
        )
        .distinct()
    )
    live_keys = {str(value) for value in live_rows if value is not None}
    if signal_ids is not None:
        live_keys &= signal_ids

    return TradeCount(
        paper_fills=len(paper_keys),
        live_submissions=len(live_keys),
        both=len(paper_keys & live_keys),
    )
