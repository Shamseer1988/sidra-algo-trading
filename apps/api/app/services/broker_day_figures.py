"""Fetch what the broker says a session was worth, and record it beside our own.

The History screen has had a four-way reconciliation since it was built, and
until now nothing wrote the broker side of it — so every live day would have
read BROKER DATA PENDING indefinitely. This is the writer.

Three things about the Upstox reports decide the shape of everything here, and
all three are properties of the broker rather than choices made in this file:

**Charges are aggregated over a date range, never per trade.** Asking for one
day gives that day's total. There is no per-trade figure available from the
broker at all, which is why a per-trade cost in this system is a local estimate
permanently, not temporarily.

**The P&L report carries no order identifiers.** It is matched buy/sell pairs by
scrip. It cannot be joined to our rows by identity, so the comparison is a total
against a total and is presented as exactly that.

**The financial year is a required parameter and India's runs April to March.**
A session on 2026-03-31 belongs to FY 2025-26 and one on 2026-04-01 to 2026-27;
getting that wrong returns an empty report rather than an error, which would
look like a day with no trades.

Nothing here writes to paper_fills, paper_positions or paper_orders. The
snapshot is appended and compared; the local record of what this system believed
at the time is never edited.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BrokerDaySnapshot

UPSTOX = "UPSTOX"
SOURCE = "upstox:trade/profit-loss"

# Upstox pages the P&L report. A single session will not fill one page, but the
# loop is bounded anyway: a broker that ignored page_number would otherwise
# return the same page forever.
MAX_PAGES = 20
PAGE_SIZE = 100


def financial_year(session_date: date) -> str:
    """India's FY as Upstox wants it: 2025-04-06 -> "2526".

    April to March. A date in January, February or March belongs to the year
    that started the previous April, which is the half of this rule that gets
    written wrong.
    """
    start = session_date.year if session_date.month >= 4 else session_date.year - 1
    return f"{start % 100:02d}{(start + 1) % 100:02d}"


def _number(value: Any) -> Decimal | None:
    """A Decimal, or None. Never zero as a stand-in for absent.

    The distinction carries all the way to the screen: a broker that did not
    report a figure and a broker that reported zero are different claims, and
    only one of them is worth reconciling against.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _sum(values: list[Decimal | None]) -> Decimal | None:
    present = [value for value in values if value is not None]
    return sum(present, start=Decimal("0")) if present else None


@dataclass(frozen=True)
class DayFigures:
    """One session as the broker reports it."""

    realized_pnl: Decimal | None
    charges: Decimal | None
    turnover: Decimal | None
    trade_count: int | None
    payload: dict


def read_profit_loss(rows: list[dict]) -> tuple[Decimal | None, Decimal | None, int]:
    """Realised P&L and turnover from the matched-pair rows.

    Upstox does not label a single "realised" field on these rows; it reports
    each matched pair's buy and sell amounts. The realised figure is the
    difference, which is why it is computed here rather than read. Rows that
    carry neither amount contribute nothing instead of contributing zero.
    """
    realised: list[Decimal | None] = []
    turnover: list[Decimal | None] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        buy = _number(row.get("buy_amount"))
        sell = _number(row.get("sell_amount"))
        if buy is None and sell is None:
            continue
        realised.append((sell or Decimal("0")) - (buy or Decimal("0")))
        turnover.append((sell or Decimal("0")) + (buy or Decimal("0")))
    return _sum(realised), _sum(turnover), len(realised)


def read_charges(payload: dict) -> Decimal | None:
    """The total charge figure, from whichever shape the response uses.

    The documented body nests a breakdown under ``charges_breakdown`` with a
    ``total``. Both the nested total and a flat one are accepted, and when
    neither is present the individual lines are added instead — a response that
    itemises without totalling is still an answer.
    """
    if not isinstance(payload, dict):
        return None
    breakdown = payload.get("charges_breakdown")
    if isinstance(breakdown, dict):
        total = _number(breakdown.get("total"))
        if total is not None:
            return total
        lines: list[Decimal | None] = [_number(breakdown.get("brokerage"))]
        for group in ("taxes", "charges"):
            nested = breakdown.get(group)
            if isinstance(nested, dict):
                lines.extend(_number(value) for value in nested.values())
        return _sum(lines)
    return _number(payload.get("total"))


def settled_charges(total: Decimal | None) -> Decimal | None:
    """A charges total, unless it is exactly zero -- which is not a cost.

    Upstox answers both of these reports before the day is settled, and an
    unsettled day comes back as an empty P&L report and a charges breakdown
    that totals zero. Read literally that is the broker saying a session cost
    nothing, and the History screen said so: "UPSTOX charged ₹0 against our
    estimate of ₹25.94. The broker's figure is the real cost." It is not. No
    executed equity trade in India costs nothing -- STT, exchange transaction
    charges, the SEBI turnover fee, stamp duty and GST are each non-zero and
    none of them are waivable -- so a zero total is a figure that has not been
    computed yet.

    The same distinction this module already draws everywhere else: a broker
    that did not report a figure is not a broker reporting zero. Returning None
    leaves the day BROKER DATA PENDING, which is true and self-correcting --
    fetched again after settlement it picks up the real cost.

    A genuine zero would be indistinguishable from this, and that is accepted:
    a day of trading that truly cost nothing does not exist here, while a day
    reported as free when it was not is a figure an operator would plan around.
    """
    return None if total is not None and total == 0 else total


async def fetch_rows(
    client, from_date: date, to_date: date, *, financial_year_code: str, segment: str = "EQ"
) -> list[dict]:
    """Every matched pair in a range, paged until the broker stops sending.

    One call covers a range, which is what makes a backfill cheap: a month of
    sessions is one request for the trades, not one per day. Only the charges
    have to be asked for a day at a time, because Upstox aggregates them over
    whatever range it is given and there is no way to split the total back out.
    """
    rows: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        batch = await client.trade_profit_loss(
            from_date=from_date,
            to_date=to_date,
            financial_year=financial_year_code,
            segment=segment,
            page_number=page,
            page_size=PAGE_SIZE,
        )
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
    return rows


def figures_from(rows: list[dict], charges_body: dict, *, financial_year_code: str, segment: str) -> DayFigures:
    """One day's rows and one day's charges, read into the figures we store."""
    realised, turnover, count = read_profit_loss(rows)
    return DayFigures(
        realized_pnl=realised,
        charges=settled_charges(read_charges(charges_body)),
        turnover=turnover,
        trade_count=count or None,
        # Kept whole so a disagreement can be investigated against what the
        # broker actually sent, rather than against our reading of it -- and so
        # the per-trade rows survive for the screen to read later.
        payload={
            "financial_year": financial_year_code,
            "segment": segment,
            "rows": rows,
            "charges": charges_body,
        },
    )


async def fetch_upstox_day(client, session_date: date, *, segment: str = "EQ") -> DayFigures:
    """Ask Upstox what one session was worth.

    ``client`` is the read-only report client; nothing on this path can place,
    modify or cancel an order, which is what makes it safe to run on a
    scheduler against a live account.
    """
    year = financial_year(session_date)
    rows = await fetch_rows(client, session_date, session_date, financial_year_code=year, segment=segment)
    charges_body = await client.trade_charges(
        from_date=session_date, to_date=session_date, financial_year=year, segment=segment
    )
    return figures_from(rows, charges_body, financial_year_code=year, segment=segment)


async def record(
    session: AsyncSession, session_date: date, figures: DayFigures, *, broker: str = UPSTOX, source: str = SOURCE
) -> BrokerDaySnapshot:
    """Append the snapshot. The caller commits.

    Append, never update. A broker's own figures settle over hours, and a row
    that was overwritten would lose the fact that they moved — which is exactly
    the evidence somebody needs when a day flips from MISMATCH to MATCHED.
    """
    snapshot = BrokerDaySnapshot(
        session_date=session_date,
        broker=broker,
        source=source,
        realized_pnl=figures.realized_pnl,
        charges=figures.charges,
        turnover=figures.turnover,
        trade_count=figures.trade_count,
        payload=figures.payload,
    )
    session.add(snapshot)
    return snapshot


# --- which session a row belongs to ---------------------------------------

# Upstox sends dates as strings and does not say which layout. These are the
# ones it has been seen to use, ISO first because a four-digit leading year
# cannot be anything else -- "01-10-2026" read the wrong way round is a real
# date in a different month, which is the kind of mistake that files a trade
# under January and is never noticed.
_ROW_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d")


def row_date(value: Any) -> date | None:
    """The session a matched pair closed on, or None if it cannot be read."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    # An ISO timestamp, trimmed to its date half.
    head = text.split("T")[0].split(" ")[0]
    for layout in _ROW_DATE_FORMATS:
        try:
            return datetime.strptime(head, layout).date()
        except ValueError:
            continue
    return None


def group_by_session(rows: list[dict]) -> tuple[dict[date, list[dict]], list[dict]]:
    """Rows filed under the session they closed on, plus the ones that would not file.

    Keyed on the sell date, falling back to the buy date. An intraday round trip
    opens and closes on the same session so the two agree; for anything carried
    overnight the sell is when the money was realised, which is the day a P&L
    report is reporting on.

    Unreadable rows are returned rather than dropped. A row silently discarded
    is a day that quietly disagrees with the broker's own screen, and the
    disagreement is the thing this whole path exists to surface.
    """
    grouped: dict[date, list[dict]] = {}
    unfiled: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        session = row_date(row.get("sell_date")) or row_date(row.get("buy_date"))
        if session is None:
            unfiled.append(row)
            continue
        grouped.setdefault(session, []).append(row)
    return grouped, unfiled


def financial_year_spans(from_date: date, to_date: date) -> list[tuple[str, date, date]]:
    """The range split at the April boundary, one span per financial year.

    Upstox takes the financial year as a required parameter, so a range that
    crosses 31 March is two requests. Asking with one year for both halves
    returns an empty report for the half that does not belong to it -- no error,
    just a month that looks as though nothing was traded in it.
    """
    spans: list[tuple[str, date, date]] = []
    cursor = from_date
    while cursor <= to_date:
        year = financial_year(cursor)
        # The last day of this financial year: 31 March of the following year.
        start_year = cursor.year if cursor.month >= 4 else cursor.year - 1
        year_end = date(start_year + 1, 3, 31)
        span_end = min(year_end, to_date)
        spans.append((year, cursor, span_end))
        cursor = span_end + timedelta(days=1)
    return spans


# --- keeping the local copy level with the broker -------------------------


@dataclass
class SyncReport:
    """What one sync did, in the terms an operator asked the question in."""

    requests: int = 0
    days_seen: list[date] = field(default_factory=list)
    days_recorded: list[date] = field(default_factory=list)
    days_skipped: list[date] = field(default_factory=list)
    unfiled_rows: int = 0

    @property
    def recorded(self) -> int:
        return len(self.days_recorded)


async def _newest_charges(
    session: AsyncSession, from_date: date, to_date: date, *, broker: str
) -> dict[date, Decimal | None]:
    """The charges figure on the newest snapshot of each date in the range."""
    rows = (
        await session.execute(
            select(BrokerDaySnapshot.session_date, BrokerDaySnapshot.charges)
            .where(
                BrokerDaySnapshot.session_date >= from_date,
                BrokerDaySnapshot.session_date <= to_date,
                BrokerDaySnapshot.broker == broker,
            )
            .order_by(BrokerDaySnapshot.session_date.asc(), BrokerDaySnapshot.fetched_at.asc())
        )
    ).all()
    # Ascending, so the newest write per date is the one left standing.
    newest: dict[date, Decimal | None] = {}
    for session_date, charges in rows:
        newest[session_date] = charges
    return newest


async def settled_dates(session: AsyncSession, from_date: date, to_date: date, *, broker: str = UPSTOX) -> set[date]:
    """Dates whose newest snapshot already carries the broker's charges.

    These are finished. A broker's figures settle once and then stop moving, so
    asking again spends a request to be told the same thing -- which is the
    whole of the difference between a sync that costs two calls a day and one
    that costs two calls per day per run forever.
    """
    newest = await _newest_charges(session, from_date, to_date, broker=broker)
    return {session_date for session_date, charges in newest.items() if charges is not None}


async def dates_awaiting_charges(
    session: AsyncSession, from_date: date, to_date: date, *, broker: str = UPSTOX
) -> set[date]:
    """Dates we have a snapshot for whose charges the broker has not published.

    The question the morning pass asks before spending anything: is there a day
    in recent memory still waiting? A deployment with nothing outstanding makes
    no request at all.
    """
    newest = await _newest_charges(session, from_date, to_date, broker=broker)
    return {session_date for session_date, charges in newest.items() if charges is None}


async def sync_days(
    db: AsyncSession,
    client,
    from_date: date,
    to_date: date,
    *,
    segment: str = "EQ",
    force: bool = False,
    broker: str = UPSTOX,
    source: str = SOURCE,
) -> SyncReport:
    """Bring the local snapshots level with the broker over a date range.

    One mechanism, used three ways: the evening job runs it over today, the
    morning job over the last few sessions, and the backfill over a year. They
    differ only in the range, which is the reason there is one of these rather
    than three jobs that drifted apart.

    The request count is the point of the shape. Trades come back for the whole
    range in one call; charges are asked for only on the days that still need
    them, and a day whose charges have already settled is never asked about
    again. A week of sessions already settled costs one request; a single
    unsettled day costs two.

    Read-only at the broker, and append-only here. Nothing in this function can
    place an order or edit a local record.
    """
    report = SyncReport()
    done = set() if force else await settled_dates(db, from_date, to_date, broker=broker)

    for year, span_start, span_end in financial_year_spans(from_date, to_date):
        rows = await fetch_rows(client, span_start, span_end, financial_year_code=year, segment=segment)
        report.requests += 1
        grouped, unfiled = group_by_session(rows)
        report.unfiled_rows += len(unfiled)

        for session_date in sorted(grouped):
            # A row can carry a sell date outside the span we asked for; filing
            # it under a day we were not asked about would write a snapshot
            # built from part of that day's trades.
            if not (from_date <= session_date <= to_date):
                continue
            report.days_seen.append(session_date)
            if session_date in done:
                report.days_skipped.append(session_date)
                continue
            charges_body = await client.trade_charges(
                from_date=session_date, to_date=session_date, financial_year=year, segment=segment
            )
            report.requests += 1
            figures = figures_from(grouped[session_date], charges_body, financial_year_code=year, segment=segment)
            await record(db, session_date, figures, broker=broker, source=source)
            report.days_recorded.append(session_date)

    return report
