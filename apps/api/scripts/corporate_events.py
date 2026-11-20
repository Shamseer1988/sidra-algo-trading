"""List or record splits, bonuses, dividends and results dates for the swing desk.

Listing is read-only. Recording writes one row to ``corporate_events``, and
only with ``--save``; without it the row is shown and nothing is written.

    # read-only
    docker compose exec -T api python scripts/corporate_events.py list
    docker compose exec -T api python scripts/corporate_events.py list --symbol TATAMOTORS

    # preview, then save, a confirmed 1:2 split (prices before the ex-date halve)
    docker compose exec -T api python scripts/corporate_events.py add --symbol XYZ --type SPLIT --ex-date 2025-06-12 --factor 2
    docker compose exec -T api python scripts/corporate_events.py add --symbol XYZ --type SPLIT --ex-date 2025-06-12 --factor 2 --save

    # a results date, so the swing desk stays out of the share around it
    docker compose exec -T api python scripts/corporate_events.py add --symbol XYZ --type RESULTS --ex-date 2026-01-22 --save

``--factor`` is what prices before the ex-date are divided by: 2 for a 1:2
split or a 1:1 bonus, 1.5 for a 1:2 bonus, 5 for a 1:5 split. Rows recorded
here are marked confirmed, because a person is recording them; suspected splits
found in the data are only ever listed (scripts/swing_data_coverage.py).

Swing paper trading reads this table. The intraday desk does not.
"""

import argparse
import asyncio
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TYPES = ("SPLIT", "BONUS", "DIVIDEND", "RESULTS")


def parse_day(value: str):
    return datetime.strptime(value, "%Y-%m-%d").date()


def validated(event_type: str, factor: Decimal | None, amount: Decimal | None) -> str | None:
    """Why this row would be wrong, or None."""
    if event_type in ("SPLIT", "BONUS"):
        if factor is None or factor <= 1:
            return f"A {event_type} needs --factor greater than 1 (prices before the ex-date are divided by it)."
    elif factor is not None:
        return f"A {event_type} does not adjust prices; leave out --factor."
    if event_type == "DIVIDEND" and (amount is None or amount <= 0):
        return "A DIVIDEND needs --amount per share."
    return None


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--symbol")
    adding = commands.add_parser("add")
    adding.add_argument("--symbol", required=True)
    adding.add_argument("--type", required=True, choices=TYPES)
    adding.add_argument("--ex-date", required=True, type=parse_day)
    adding.add_argument("--factor")
    adding.add_argument("--amount")
    adding.add_argument("--note")
    adding.add_argument("--save", action="store_true")
    args = parser.parse_args()

    from sqlalchemy import select

    from app.db.models import CorporateEvent
    from app.db.session import SessionLocal
    from app.services.swing_universe import nifty200

    members = {member.symbol: member for member in nifty200()}

    async with SessionLocal() as session:
        if args.command == "list":
            query = select(CorporateEvent).order_by(CorporateEvent.ex_date.desc())
            if args.symbol:
                query = query.where(CorporateEvent.symbol == args.symbol.upper())
            rows = (await session.scalars(query)).all()
            if not rows:
                print("No corporate events recorded.")
            for row in rows:
                detail = f"x{row.price_factor}" if row.price_factor else (f"Rs {row.amount}" if row.amount else "")
                print(
                    f"  {row.ex_date}  {row.symbol or row.instrument_token:<14} {row.event_type:<9} {detail:<12}"
                    f" {'confirmed' if row.confirmed else 'suspected'}  {row.note or ''}"
                )
            return 0

        symbol = args.symbol.upper()
        member = members.get(symbol)
        if member is None:
            print(f"{symbol} is not in the NIFTY 200 list the swing desk uses; nothing was written.")
            return 1
        try:
            factor = Decimal(args.factor) if args.factor else None
            amount = Decimal(args.amount) if args.amount else None
        except InvalidOperation:
            print("--factor and --amount must be numbers; nothing was written.")
            return 1
        problem = validated(args.type, factor, amount)
        if problem:
            print(problem + " Nothing was written.")
            return 1

        row = CorporateEvent(
            instrument_token=member.instrument_token,
            symbol=symbol,
            event_type=args.type,
            ex_date=args.ex_date,
            price_factor=factor,
            amount=amount,
            confirmed=True,
            source="manual",
            note=args.note,
        )
        print(f"  {args.ex_date}  {symbol}  {args.type}  factor={factor}  amount={amount}  ({member.instrument_token})")
        if not args.save:
            print("  Preview only. Add --save to record it.")
            return 0
        session.add(row)
        await session.commit()
        print("  Saved.")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
