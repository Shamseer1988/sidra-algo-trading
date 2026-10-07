"""Say how a live trade ended, every time one does.

On 7 October an IRCTC short was opened automatically, the operator got a
message saying so, and the stop filled an hour later. No message was sent. The
first they knew of it was the broker's app.

Nothing was broken, which is the point. The exit sweep announces the exits *it*
performs — a target, a square-off, a position it had to flatten — and a stop
resting at the broker is none of those. It fills at the exchange, the position
goes to zero, and the next sweep skips a flat row. The single most common way a
trade ends was the one way that produced silence.

So the end of a trade is detected where it actually happens: in the broker's
order book, which the sweep already reads once a minute. An exit order that has
filled is a trade that has ended, whoever closed it and whatever closed it.

Three things this module is careful about.

**It sends once.** The ledger is an audit row per signal, so a restart, a second
sweep or a re-read of the same book cannot produce a second message about the
same trade. Audit rather than Redis because a flush must not re-announce a day
of trades.

**It uses the broker's fills, not ours.** Entry and exit prices come off the
order book, so the figure in the message is the figure in the account. A
modelled price here would be a message that disagrees with the broker's app,
which is the thing the operator opens next.

**It says how the trade ended, not just that it did.** "Stop hit" and "target
reached" are the same money and completely different information, and an
operator reading a week of these is reading them for that.
"""

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, LiveOrderSubmission

logger = logging.getLogger(__name__)

ALERT_EVENT = "live.trade_closed"

STOP_MARKET = "SL-M"
STOP_LIMIT = "SL"

# How a trade ended, in the words the message uses.
BY_STOP = "Stop hit"
BY_EXIT = "Closed by the system"


@dataclass(frozen=True)
class ClosedTrade:
    """One live round trip, as the broker's own book reports it."""

    signal_id: UUID | None
    symbol: str
    side: str
    quantity: int
    entry_price: Decimal
    exit_price: Decimal
    gross: Decimal
    ending: str
    exit_order_id: str

    @property
    def won(self) -> bool:
        return self.gross > 0


def _filled(record: Any) -> bool:
    """Did this order-book row actually trade?

    Quantity, not status: the brokers spell "complete" differently and a
    partially filled exit is still an exit that moved money.
    """
    return bool(record.filled_quantity) and record.average_price is not None


def closed_trades(submissions: list[LiveOrderSubmission], book: list) -> list[ClosedTrade]:
    """Every round trip the book shows as finished, from our own orders.

    Pure, so the classification can be tested exhaustively without a broker.

    The earliest submission for a signal is its entry — this system always
    places the entry first and everything after it closes the position — so the
    direction comes from that row rather than from a signal lookup, and a trade
    whose signal has since been deleted still reports correctly.
    """
    by_id = {str(record.broker_order_id): record for record in book if record.broker_order_id}
    per_signal: dict[Any, list[LiveOrderSubmission]] = {}
    for row in submissions:
        key = row.paper_signal_id or row.id
        per_signal.setdefault(key, []).append(row)

    closed: list[ClosedTrade] = []
    for rows in per_signal.values():
        ordered = sorted(rows, key=lambda row: row.created_at)
        entry_row, *rest = ordered
        entry = next((by_id[number] for number in _numbers(entry_row) if number in by_id), None)
        if entry is None or not _filled(entry):
            continue
        long = (entry_row.canonical_side or "").upper() == "BUY"

        for row in rest:
            record = next((by_id[number] for number in _numbers(row) if number in by_id), None)
            if record is None or not _filled(record):
                continue
            exit_price = Decimal(str(record.average_price))
            entry_price = Decimal(str(entry.average_price))
            quantity = int(record.filled_quantity or 0)
            difference = (exit_price - entry_price) if long else (entry_price - exit_price)
            closed.append(
                ClosedTrade(
                    signal_id=entry_row.paper_signal_id,
                    symbol=entry_row.trading_symbol,
                    side="LONG" if long else "SHORT",
                    quantity=quantity,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    gross=(difference * quantity).quantize(Decimal("0.01")),
                    ending=BY_STOP if (row.canonical_order_type in {STOP_MARKET, STOP_LIMIT}) else BY_EXIT,
                    exit_order_id=str(record.broker_order_id),
                )
            )
    return closed


def _numbers(row: LiveOrderSubmission) -> list[str]:
    return [str(number) for number in (row.broker_order_numbers or []) if number]


async def unannounced(session: AsyncSession, trades: list[ClosedTrade]) -> list[ClosedTrade]:
    """The trades nobody has been told about yet.

    Keyed on the exit order rather than on the signal: a trade closed in two
    fills is two movements of money, and an operator who sees one message for
    the first half and silence for the second has been told something false.
    """
    if not trades:
        return []
    rows = await session.scalars(
        select(AuditLog.metadata_json).where(AuditLog.event_type == ALERT_EVENT),
    )
    sent = {str((row or {}).get("exit_order_id")) for row in rows.all()}
    return [trade for trade in trades if trade.exit_order_id not in sent]


async def record_announced(session: AsyncSession, trade: ClosedTrade) -> None:
    """Write the ledger entry that stops this being sent twice."""
    session.add(
        AuditLog(
            user_id=None,
            event_type=ALERT_EVENT,
            metadata_json={
                "exit_order_id": trade.exit_order_id,
                "signal_id": str(trade.signal_id) if trade.signal_id else None,
                "symbol": trade.symbol,
                "gross": str(trade.gross),
                "ending": trade.ending,
            },
        )
    )


async def todays_submissions(session: AsyncSession, start, end) -> list[LiveOrderSubmission]:  # noqa: ANN001
    """Every live order of this session, whatever became of it."""
    rows = await session.scalars(
        select(LiveOrderSubmission)
        .where(LiveOrderSubmission.created_at >= start, LiveOrderSubmission.created_at < end)
        .order_by(LiveOrderSubmission.created_at.asc())
    )
    return list(rows.all())


def message(trade: ClosedTrade, session_date: date, day_pnl: Decimal | None) -> str:
    """What the operator reads. HTML, as every other alert in this system is."""
    icon = "\U0001f7e2" if trade.won else "\U0001f534"
    sign = "+" if trade.gross >= 0 else "−"
    amount = f"{sign}₹{abs(trade.gross):,.2f}"
    lines = [
        f"{icon} <b>TRADE CLOSED</b> — {trade.ending}",
        "",
        f"\U0001f4ca {trade.symbol}  {trade.side}  {trade.quantity}",
        f"\U0001f4b5 Entry {trade.entry_price:,.2f}  →  Exit {trade.exit_price:,.2f}",
        f"\U0001f4b0 Gross {amount}  <i>(before charges)</i>",
    ]
    if day_pnl is not None:
        running = f"{'+' if day_pnl >= 0 else '−'}₹{abs(day_pnl):,.2f}"
        lines.append(f"\U0001f4c5 Account today: {running}")
    lines.append("")
    lines.append(f"<i>{session_date} — broker order {trade.exit_order_id}</i>")
    return "\n".join(lines)
