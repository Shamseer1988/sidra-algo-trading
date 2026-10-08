"""Everything that happened on the live path for one session, in one place.

Written for the morning after a trade behaved in a way the Telegram messages
did not explain. It answers four questions that currently need the broker's app
open beside the container logs:

  what did we send      price, trigger, quantity, and the words we sent them in
  what did they say     the broker's own status and its own refusal message
  what actually filled  quantity and average price, per leg
  what did it risk      planned against sent, and budget against the real stop
  what is a stop really the limit it rests on, which is not always the kind of
                        order we asked for, and when the exchange acted on it

Read-only everywhere. The broker side goes through the report client, which has
no method that can place, modify or cancel an order, so this is safe to run
against a live account during a session.

Usage:

    python scripts/inspect_live_day.py                 # today, IST
    python scripts/inspect_live_day.py --date 2026-10-07
    python scripts/inspect_live_day.py --no-broker     # local records only
"""

import argparse
import asyncio
import sys
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.models import LiveOrderSubmission, PaperSignal  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services.trade_counter import session_bounds_utc  # noqa: E402
from app.services.trading_calendar import MARKET_TIMEZONE  # noqa: E402

RULE = "=" * 78
THIN = "-" * 78


def parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def stop_shape(record) -> str | None:  # noqa: ANN001
    """Say what a stop is actually resting as, when that is not what we asked.

    Every protective stop this system places goes out as SL-M, and on 8 October
    Upstox's order book answered "SL" for one and "LIMIT" for another. Neither
    is SL-M, and the difference matters: a market stop fills at whatever the
    book offers, while a stop resting on a limit can be jumped and leave the
    position open with the loss still running.

    The limit price settles it, so it is read rather than reasoned about. A
    protected market order carries a band Upstox computed; a plain limit stop
    carries its own trigger; a true market stop carries nothing.
    """
    trigger = record.trigger_price or Decimal("0")
    if trigger <= 0:
        return None
    limit = record.limit_price
    if limit is None:
        return "broker did not report a limit price"
    if limit <= 0:
        return "resting with no limit - fills at market once triggered"
    if limit == trigger:
        return f"limit equals the trigger ({money(limit)}) - will not fill past it"
    return f"limit {money(limit)} against trigger {money(trigger)} - a protection band of {money(abs(limit - trigger))}"


def money(value) -> str:  # noqa: ANN001
    if value is None:
        return "—"
    try:
        return f"{Decimal(str(value)):,.2f}"
    except (InvalidOperation, ValueError):
        return str(value)


def ist(value: datetime | None) -> str:
    return value.astimezone(MARKET_TIMEZONE).strftime("%H:%M:%S") if value else "—"


def canonical(row: LiveOrderSubmission, key: str) -> str:
    return str(((row.request_snapshot or {}).get("canonical") or {}).get(key) or "—")


async def main(args) -> int:
    settings = get_settings()
    session_date = args.date or datetime.now(MARKET_TIMEZONE).date()
    start, end = session_bounds_utc(session_date)

    async with SessionLocal() as session:
        rows = list(
            await session.scalars(
                select(LiveOrderSubmission)
                .where(LiveOrderSubmission.created_at >= start, LiveOrderSubmission.created_at < end)
                .order_by(LiveOrderSubmission.created_at.asc())
            )
        )
        signal_ids = {row.paper_signal_id for row in rows if row.paper_signal_id}
        signals = (
            {item.id: item for item in await session.scalars(select(PaperSignal).where(PaperSignal.id.in_(signal_ids)))}
            if signal_ids
            else {}
        )

    print(RULE)
    print(f"LIVE SESSION {session_date}   {len(rows)} submission(s)")
    print(RULE)
    if not rows:
        print("  Nothing was sent to a broker on this date.")
        return 0

    # --- what we sent, and what we were told ---------------------------------
    for row in rows:
        print()
        print(
            f"{ist(row.created_at)}  {row.trading_symbol}  {canonical(row, 'side')} "
            f"{canonical(row, 'orderType')}  qty {row.quantity}"
        )
        print(
            f"    we sent      price {money(row.price)}  trigger {money(row.trigger_price)}  "
            f"product {row.product}  broker words: {row.transaction_type}/{row.price_type}"
        )
        print(f"    our status   {row.status}   order id(s): {', '.join(row.broker_order_numbers or []) or '—'}")
        print(
            f"    broker says  status {row.broker_status or '—'}   filled {row.filled_quantity if row.filled_quantity is not None else '—'}"
            f"  at {money(row.average_fill_price)}"
        )
        if row.failure_message or row.failure_name or row.failure_code:
            print(
                f"    REFUSAL      [{row.failure_name or ''}{'/' if row.failure_name and row.failure_code else ''}"
                f"{row.failure_code or ''}] {row.failure_message or ''}"
            )
        if row.resolution_detail:
            print(f"    resolution   {row.resolution_detail}")

    # --- what the trade was supposed to risk, against what it did ------------
    print()
    print(RULE)
    print("SIZING — what the strategy planned against what reached the broker")
    print(RULE)
    entries = [row for row in rows if canonical(row, "orderType") in {"LIMIT", "MARKET"} and row.paper_signal_id]
    seen: set = set()
    for row in entries:
        signal = signals.get(row.paper_signal_id)
        if signal is None or signal.id in seen:
            continue
        seen.add(signal.id)
        entry = Decimal(str(signal.entry_price))
        stop = Decimal(str(signal.stop_price))
        planned_room = abs(stop - entry)
        sent_room = abs(stop - Decimal(str(row.price))) if row.price else None
        filled = row.average_fill_price
        real_room = abs(stop - Decimal(str(filled))) if filled else None
        budget = Decimal(str(signal.risk_amount))

        print()
        print(f"  {signal.instrument_token}  {signal.side}")
        print(f"    budget            ₹{money(budget)}")
        print(f"    signal entry      {money(entry)}   stop {money(stop)}   per share ₹{money(planned_room)}")
        print(f"    planned quantity  {signal.quantity}   -> risk ₹{money(planned_room * signal.quantity)}")
        if sent_room:
            print(f"    limit sent        {money(row.price)}   per share ₹{money(sent_room)}")
        print(
            f"    quantity sent     {row.quantity}"
            f"   -> worst case ₹{money(sent_room * row.quantity) if sent_room else '—'}"
        )
        if real_room:
            print(f"    filled at         {money(filled)}   per share ₹{money(real_room)}")
            print(
                f"    REAL risk         ₹{money(real_room * row.quantity)}"
                f"   ({(real_room * row.quantity / budget * 100):.0f}% of the budget)"
            )

    if args.no_broker:
        return 0

    # --- the broker's own books ---------------------------------------------
    from app.services.live_execution_gateway import live_report_adapter
    from app.services.upstox_orders import UpstoxError

    brokers = {row.broker for row in rows if row.broker} or {None}
    for broker in sorted(brokers, key=str):
        print()
        print(RULE)
        print(f"BROKER ORDER BOOK — {broker or 'default'}")
        print(RULE)
        try:
            async with SessionLocal() as session:
                adapter = await live_report_adapter(settings, session, broker)
                book = await adapter.normalised_orders()
                positions = await adapter.normalised_positions()
        except (UpstoxError, Exception) as error:  # noqa: BLE001 - reporting tool
            print(f"  [PROBLEM] could not read {broker}: {type(error).__name__}: {error}")
            continue

        ours = {str(number) for row in rows for number in (row.broker_order_numbers or [])}
        for record in book:
            mark = "*" if str(record.broker_order_id) in ours else " "
            print(
                f" {mark} {record.broker_order_id}  {record.symbol}  {record.side or '—'} "
                f"{record.order_type or '—'}  qty {record.quantity}  filled {record.filled_quantity}"
            )
            print(
                f"     status {record.status}  trigger {money(record.trigger_price)}  "
                f"limit {money(record.limit_price)}  avg {money(record.average_price)}"
            )
            # Placed against acted-on. A stop's placement time says nothing
            # about when it was hit, and without the exchange's own stamp a
            # stop that triggered in forty seconds reads the same as one that
            # held for three hours.
            print(
                f"     placed {record.placed_at or '—'}  exchange {record.exchange_at or '—'}  "
                f"tag {record.client_order_id or '—'}"
            )
            if record.status_message:
                # The field that would have said "invalid order price" in a
                # Telegram message instead of in the broker's app.
                print(f"     BROKER MESSAGE: {record.status_message}")
            note = stop_shape(record)
            if note:
                print(f"     STOP SHAPE: {note}")
        if not book:
            print("  (the order book is empty)")

        print()
        print(THIN)
        print("POSITIONS")
        print(THIN)
        for record in positions:
            print(
                f"  {record.symbol}  net {record.net_quantity}  realised {money(record.realised)}  "
                f"unrealised {money(record.unrealised)}  day {money(record.day_pnl)}"
            )
        if not positions:
            print("  (flat)")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", type=parse_day, help="Session date (YYYY-MM-DD). Default: today in IST.")
    parser.add_argument("--no-broker", action="store_true", help="Local records only; ask the broker nothing.")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
