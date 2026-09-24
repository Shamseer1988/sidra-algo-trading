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

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

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


async def fetch_upstox_day(client, session_date: date, *, segment: str = "EQ") -> DayFigures:
    """Ask Upstox what one session was worth.

    ``client`` is the read-only report client; nothing on this path can place,
    modify or cancel an order, which is what makes it safe to run on a
    scheduler against a live account.
    """
    year = financial_year(session_date)
    rows: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        batch = await client.trade_profit_loss(
            from_date=session_date,
            to_date=session_date,
            financial_year=year,
            segment=segment,
            page_number=page,
            page_size=PAGE_SIZE,
        )
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break

    realised, turnover, count = read_profit_loss(rows)
    charges_body = await client.trade_charges(
        from_date=session_date, to_date=session_date, financial_year=year, segment=segment
    )
    return DayFigures(
        realized_pnl=realised,
        charges=read_charges(charges_body),
        turnover=turnover,
        trade_count=count or None,
        # Kept whole so a disagreement can be investigated against what the
        # broker actually sent, rather than against our reading of it.
        payload={"financial_year": year, "segment": segment, "rows": rows, "charges": charges_body},
    )


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
