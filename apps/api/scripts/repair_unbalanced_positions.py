"""Make a position hold what its fills moved it by, where a duplicate fill broke that.

On 16 October a live JSWSTEEL trade ran correctly at the broker -- four
bought, four sold at the target, the resting stop cancelled -- and the
simulated ledger recorded five in and ten out. Two candles of one instrument
were in flight at once over an unlocked order row, both filled the same TARGET
order, and the position settled at a net of -5 with status REDUCING. Because
every report asks ``is_open`` before counting a trade, one real completed
winner left the record entirely while its charges stayed in the day's totals.

The engine can no longer do this: the order rows are locked while a candle is
applied, an exit fill is clamped to what the position holds, and a position at
or below zero is marked CLOSED so the counterpart is still cancelled. This
script is for the rows that were written before that.

**No fill is deleted.** The duplicate stays exactly where it is, because it is
the evidence of what happened and the daily review prints it. What is repaired
is the position, recomputed from the fills that its orders can account for --
and the exclusion is written into the order's own snapshot, naming the fill and
why it was left out. A repair that erased its own reason would be worse than
the fault.

**Reports by default. Nothing is written without --apply.** Every field it
would change is printed before and after, so the diff is read before it is
taken.

Usage:

    python scripts/repair_unbalanced_positions.py              # report only
    python scripts/repair_unbalanced_positions.py --apply      # write it
    python scripts/repair_unbalanced_positions.py --date 2026-10-16
"""

import argparse
import asyncio
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import or_, select  # noqa: E402

from app.db.models import PaperFill, PaperOrder, PaperPosition  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services.trade_history import CLOSED_STATUSES  # noqa: E402
from app.services.trading_calendar import MARKET_TIMEZONE  # noqa: E402

RULE = "=" * 92
ENTRY = "ENTRY"


def parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _money(value) -> Decimal:  # noqa: ANN001
    return Decimal(str(value or 0))


def _quantise(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"))


def authoritative(order: PaperOrder, fills: list[PaperFill]) -> tuple[list[PaperFill], list[PaperFill]]:
    """The fills one order can account for, and the ones beyond its quantity.

    An order for five cannot have filled ten. Taken in time order and capped at
    the order's own quantity, so the first fill of a duplicated pair is the one
    that counts -- it is the one whose price and time the position already
    carried, and the one the broker's single fill corresponds to.
    """
    kept: list[PaperFill] = []
    extra: list[PaperFill] = []
    running = 0
    for fill in sorted(fills, key=lambda item: item.occurred_at):
        if running + fill.quantity <= order.quantity:
            kept.append(fill)
            running += fill.quantity
        else:
            extra.append(fill)
    return kept, extra


class Recomputed:
    """What a position should hold, from the fills its orders account for."""

    def __init__(self) -> None:
        self.in_quantity = 0
        self.out_quantity = 0
        self.entry_value = Decimal("0")
        self.exit_value = Decimal("0")
        self.fees = Decimal("0")
        self.last_exit_at: datetime | None = None

    def add(self, fill: PaperFill, *, is_entry: bool) -> None:
        self.fees += _money(fill.total_fees)
        if is_entry:
            self.in_quantity += fill.quantity
            self.entry_value += _money(fill.price) * fill.quantity
        else:
            self.out_quantity += fill.quantity
            self.exit_value += _money(fill.price) * fill.quantity
            if self.last_exit_at is None or fill.occurred_at > self.last_exit_at:
                self.last_exit_at = fill.occurred_at

    @property
    def open_quantity(self) -> int:
        return self.in_quantity - self.out_quantity

    @property
    def average_entry(self) -> Decimal | None:
        return _quantise(self.entry_value / self.in_quantity) if self.in_quantity else None

    @property
    def average_exit(self) -> Decimal | None:
        return _quantise(self.exit_value / self.out_quantity) if self.out_quantity else None

    def realized(self, side: str) -> Decimal:
        """Gross on the quantity that has been closed, in the trade's direction."""
        if not self.out_quantity or self.average_entry is None or self.average_exit is None:
            return Decimal("0")
        move = self.average_exit - self.average_entry
        if side.upper() == "SHORT":
            move = -move
        return _quantise(move * self.out_quantity)


async def repair(session, position: PaperPosition, *, apply: bool) -> bool:  # noqa: ANN001
    """Report one position, and write it when asked. True if anything differs."""
    orders = list(
        (
            await session.scalars(
                select(PaperOrder)
                .where(PaperOrder.paper_signal_id == position.paper_signal_id)
                .order_by(PaperOrder.created_at.asc())
            )
        ).all()
    )
    by_id = {order.id: order for order in orders}
    fills = list(
        (
            await session.scalars(
                select(PaperFill).where(PaperFill.paper_order_id.in_(list(by_id))).order_by(PaperFill.occurred_at.asc())
            )
        ).all()
    )
    per_order: dict[UUID, list[PaperFill]] = {}
    for fill in fills:
        per_order.setdefault(fill.paper_order_id, []).append(fill)

    fresh = Recomputed()
    excluded: list[tuple[PaperOrder, PaperFill]] = []
    for order_id, order_fills in per_order.items():
        order = by_id[order_id]
        kept, extra = authoritative(order, order_fills)
        for fill in kept:
            fresh.add(fill, is_entry=order.order_role == ENTRY)
        excluded.extend((order, fill) for fill in extra)

    print(f"\n  {position.session_date}  {position.instrument_token}  {position.side}")
    print(f"    orders : {', '.join(f'{item.order_role}={item.status}/{item.filled_quantity}' for item in orders)}")
    for fill in fills:
        role = by_id[fill.paper_order_id].order_role
        mark = "EXCLUDED" if any(fill.id == item.id for _, item in excluded) else "counted "
        print(
            f"    fill   {mark} {role:<7}{fill.side:<6}{fill.quantity:>4} at {_money(fill.price):>10,.2f}  "
            f"{fill.occurred_at.astimezone(MARKET_TIMEZONE).strftime('%H:%M:%S')}"
        )
    if not excluded:
        print("    No fill exceeds its order's quantity, so the quantities are not the fault here.")
        print("    Left alone: this needs looking at by hand rather than recomputing from the same rows.")
        return False

    status = "CLOSED" if fresh.open_quantity <= 0 else ("OPEN" if fresh.out_quantity == 0 else "REDUCING")
    realized = fresh.realized(position.side)
    unrealized = Decimal("0") if fresh.open_quantity <= 0 else _money(position.unrealized_pnl)
    changes = [
        ("open_quantity", position.open_quantity, fresh.open_quantity),
        ("status", position.status, status),
        ("average_entry_price", _money(position.average_entry_price), fresh.average_entry),
        ("average_exit_price", _money(position.average_exit_price), fresh.average_exit),
        ("realized_pnl", _money(position.realized_pnl), realized),
        ("unrealized_pnl", _money(position.unrealized_pnl), unrealized),
        ("fees_total", _money(position.fees_total), _quantise(fresh.fees)),
        ("total_pnl", _money(position.total_pnl), _quantise(realized + unrealized - fresh.fees)),
        (
            "closed_at",
            position.closed_at,
            fresh.last_exit_at if fresh.open_quantity <= 0 else position.closed_at,
        ),
    ]
    print("    recomputed from the counted fills:")
    for name, before, after in changes:
        flag = " " if str(before) == str(after) else "*"
        print(f"      {flag} {name:<20} {str(before):<24} -> {after}")

    if not apply:
        print("    (report only; pass --apply to write this)")
        return True

    position.open_quantity = fresh.open_quantity
    position.status = status
    position.average_entry_price = fresh.average_entry
    position.average_exit_price = fresh.average_exit
    position.realized_pnl = realized
    position.unrealized_pnl = unrealized
    position.fees_total = _quantise(fresh.fees)
    position.total_pnl = _quantise(realized + unrealized - fresh.fees)
    if fresh.open_quantity <= 0 and fresh.last_exit_at is not None:
        position.closed_at = fresh.last_exit_at

    # The reason, written where the excluded fill lives. A repair with no record
    # of what it excluded is indistinguishable from a quiet edit of the history.
    stamped = datetime.now(MARKET_TIMEZONE).isoformat(timespec="seconds")
    for order, fill in excluded:
        notes = list((order.simulation_snapshot or {}).get("repair") or [])
        notes.append(
            {
                "excluded_fill": str(fill.id),
                "quantity": fill.quantity,
                "occurred_at": fill.occurred_at.isoformat(),
                "why": (
                    f"beyond this order's quantity of {order.quantity}; written by two candles filling one "
                    "order over an unlocked row. The fill row is kept as evidence and is not counted."
                ),
                "repaired_at": stamped,
            }
        )
        order.simulation_snapshot = {**(order.simulation_snapshot or {}), "repair": notes}
    print("    written.")
    return True


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", type=parse_day, help="One session only (YYYY-MM-DD). Default: every session.")
    parser.add_argument("--apply", action="store_true", help="Write the repair. Without it, nothing is changed.")
    args = parser.parse_args()

    print(RULE)
    print("REPAIR UNBALANCED POSITIONS" + ("" if args.apply else "   (report only)"))
    print(RULE)
    print("  A position should hold exactly what its fills moved it by. These do not.")
    print("  No fill is deleted; the position is recomputed from the fills its orders account for.")

    async with SessionLocal() as session:
        statement = select(PaperPosition).where(
            or_(
                PaperPosition.open_quantity < 0,
                PaperPosition.open_quantity == 0,
            ),
            PaperPosition.status.not_in(tuple(CLOSED_STATUSES)),
        )
        if args.date:
            statement = statement.where(PaperPosition.session_date == args.date)
        positions = list((await session.scalars(statement.order_by(PaperPosition.session_date.asc()))).all())

        if not positions:
            print("\n  Nothing to repair: every position with no quantity left is marked closed.")
            return 0

        print(f"\n  {len(positions)} position(s) hold no quantity and are not marked closed.")
        touched = 0
        for position in positions:
            if await repair(session, position, apply=args.apply):
                touched += 1
        if args.apply:
            await session.commit()

    print("\n" + RULE)
    if args.apply:
        print(f"  {touched} position(s) repaired. The excluded fills are kept, with the reason on their order.")
        print("  Re-run daily_review.py: the trade should now appear in the record.")
    else:
        print(f"  {touched} position(s) would be repaired. Nothing was changed.")
        print("  Read the lines marked * above, then re-run with --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
