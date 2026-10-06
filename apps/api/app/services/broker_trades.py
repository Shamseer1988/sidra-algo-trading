"""The broker's own trades for a session, read back out of the stored report.

Every one of these rows was already downloaded and kept. ``broker_day_figures``
asks Upstox for the matched buy/sell pairs in a range, totals them into a day
figure, and stores the response whole in ``broker_day_snapshots.payload``. Until
now only the total was ever read back; the rows sat in the payload unused.

Reading them costs nothing at the broker. There is no request here — this is a
local read of a local table, and it works for every day that was ever fetched,
including days whose local record no longer exists.

Three things these rows are not, each of which matters more than what they are:

**They are not our record.** No stop, no target, no risk amount, no strategy,
no signal. The broker reports what was bought and sold; why it was bought is
ours and is not recoverable from any API.

**A "trade" here is a matched pair, not an entry.** Upstox pairs buys against
sells per scrip over the period it was asked about. Two entries in one stock on
one day can come back as one row, so the count can legitimately differ from the
number of trades this system took.

**There is no per-trade cost.** Upstox aggregates charges over a date range and
publishes no per-trade figure, so a row carries a gross and nothing else. The
day's cost is a day-level fact and stays one.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.trade_history import latest_broker_snapshots


def _text(row: dict, *names: str) -> str | None:
    """The first of these keys that holds something, in either case convention.

    The REST responses are snake_case and the published SDKs are camelCase.
    Accepting both costs one loop and removes a whole class of silent empty
    column.
    """
    for name in names:
        for key in (name, _camel(name)):
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if value is not None and not isinstance(value, str):
                return str(value)
    return None


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


def _money(row: dict, *names: str) -> Decimal | None:
    """A Decimal, or None. Never zero as a stand-in for absent."""
    for name in names:
        for key in (name, _camel(name)):
            value = row.get(key)
            if value is None or isinstance(value, bool):
                continue
            try:
                return Decimal(str(value))
            except (InvalidOperation, ValueError, TypeError):
                continue
    return None


def _count(row: dict, *names: str) -> int | None:
    value = _money(row, *names)
    return None if value is None else int(value)


@dataclass(frozen=True)
class BrokerTrade:
    """One matched buy/sell pair, as the broker reported it."""

    session_date: date
    broker: str
    script_name: str
    isin: str | None
    trade_type: str | None
    quantity: int | None
    buy_price: Decimal | None
    sell_price: Decimal | None
    gross_pnl: Decimal | None
    fetched_at: datetime


def read_row(row: Any, *, session_date: date, broker: str, fetched_at: datetime) -> BrokerTrade | None:
    """One report row, or None if it carries nothing worth showing."""
    if not isinstance(row, dict):
        return None
    name = _text(row, "scrip_name", "script_name", "symbol")
    buy = _money(row, "buy_amount")
    sell = _money(row, "sell_amount")
    if name is None and buy is None and sell is None:
        return None
    # Gross is the difference between what the pair sold for and what it cost,
    # which is how the broker's own report expects to be read: it publishes the
    # two amounts and no P&L column. A pair missing one side has no gross rather
    # than a gross computed against zero.
    gross = None if (buy is None and sell is None) else (sell or Decimal("0")) - (buy or Decimal("0"))
    return BrokerTrade(
        session_date=session_date,
        broker=broker,
        script_name=name or "—",
        isin=_text(row, "isin"),
        trade_type=_text(row, "trade_type"),
        quantity=_count(row, "quantity"),
        buy_price=_money(row, "buy_average", "buy_average_price"),
        sell_price=_money(row, "sell_average", "sell_average_price"),
        gross_pnl=gross,
        fetched_at=fetched_at,
    )


def read_snapshot_rows(snapshot) -> list[dict]:
    rows = (snapshot.payload or {}).get("rows")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


async def load_broker_trades(
    session: AsyncSession, from_date: date, to_date: date, *, broker: str | None = None
) -> list[BrokerTrade]:
    """Every matched pair the broker reported in the range, newest day first.

    Read from the newest snapshot per date, because the older ones are the
    record of how the broker's figures settled rather than a second set of
    trades. Summing across snapshots would count a re-fetched day twice.
    """
    snapshots = await latest_broker_snapshots(session, from_date, to_date, broker=broker)
    trades: list[BrokerTrade] = []
    for session_date, snapshot in snapshots.items():
        for row in read_snapshot_rows(snapshot):
            parsed = read_row(row, session_date=session_date, broker=snapshot.broker, fetched_at=snapshot.fetched_at)
            if parsed is not None:
                trades.append(parsed)
    return sorted(trades, key=lambda trade: (trade.session_date, trade.script_name), reverse=True)
