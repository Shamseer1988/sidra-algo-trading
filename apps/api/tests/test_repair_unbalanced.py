"""Which fill a duplicated pair keeps, and what the position should then hold.

The repair recomputes a position from the fills its orders can account for. Two
decisions in that sentence are the whole of it, and both are testable without a
database:

**An order for five cannot have filled ten.** Taken in time order and capped at
the order's own quantity, so the *first* fill of a duplicated pair is the one
that counts. It has to be the first: it is the fill whose price and time the
position already carried, and the one the broker's single real fill corresponds
to. Keeping the later one would move the trade's exit time by however long the
duplicate took to arrive.

**No fill is deleted.** The duplicate stays as the evidence; only the position
is recomputed. So a repair must be able to say which fill it left out, and
these tests pin that it is identified rather than inferred.
"""

import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import repair_unbalanced_positions as repair  # noqa: E402

START = datetime(2026, 10, 16, 4, 27, tzinfo=UTC)


def order(quantity: int = 5):
    return SimpleNamespace(id=uuid4(), order_role="TARGET", quantity=quantity, status="FILLED")


def fill(quantity: int, *, minute: int, price: str = "1227.39", fees: str = "3.465"):
    return SimpleNamespace(
        id=uuid4(),
        quantity=quantity,
        price=Decimal(price),
        total_fees=Decimal(fees),
        occurred_at=START + timedelta(minutes=minute),
    )


# --- which fills an order can account for ----------------------------------


def test_a_single_fill_is_all_counted():
    kept, extra = repair.authoritative(order(5), [fill(5, minute=0)])
    assert len(kept) == 1 and extra == []


def test_the_second_of_a_duplicated_pair_is_the_one_excluded():
    """The real case: one TARGET order for five, filled for five twice, a minute
    apart. The earlier fill is the one the position already carried."""
    first, second = fill(5, minute=74), fill(5, minute=75)
    kept, extra = repair.authoritative(order(5), [first, second])
    assert [item.id for item in kept] == [first.id]
    assert [item.id for item in extra] == [second.id]


def test_the_split_does_not_depend_on_the_order_the_rows_arrive_in():
    """Sorted by time inside, because a database returns rows in whatever order
    it likes and the repair must not depend on that."""
    first, second = fill(5, minute=74), fill(5, minute=75)
    kept, _ = repair.authoritative(order(5), [second, first])
    assert [item.id for item in kept] == [first.id]


def test_two_genuine_partial_fills_are_both_counted():
    """A partial fill followed by the rest is not a duplicate. Capping at the
    order's quantity has to leave this case alone, or the repair would delete
    half of every sliced exit."""
    kept, extra = repair.authoritative(order(5), [fill(2, minute=0), fill(3, minute=1)])
    assert sum(item.quantity for item in kept) == 5
    assert extra == []


def test_a_partial_duplicate_is_excluded_whole():
    """Three counted and three more when only five were ordered: the second
    fill cannot be split, so it is excluded rather than trimmed. A trimmed fill
    would be a row this script invented."""
    kept, extra = repair.authoritative(order(5), [fill(3, minute=0), fill(3, minute=1)])
    assert sum(item.quantity for item in kept) == 3
    assert len(extra) == 1


# --- what the position should then hold ------------------------------------


def recomputed(*, side: str = "LONG", exits: int = 1):
    fresh = repair.Recomputed()
    fresh.add(fill(5, minute=0, price="1208.34", fees="3.46"), is_entry=True)
    for index in range(exits):
        fresh.add(fill(5, minute=74 + index, price="1227.39"), is_entry=False)
    return fresh


def test_a_round_trip_leaves_the_position_flat():
    fresh = recomputed()
    assert fresh.open_quantity == 0
    assert fresh.average_entry == Decimal("1208.3400")
    assert fresh.average_exit == Decimal("1227.3900")


def test_the_gain_is_counted_once_not_once_per_duplicate():
    """The fault inflated the realised figure as well as the quantity: the
    JSWSTEEL row held 190.50 for a trade that made 95.25, because both fills
    were added. Had the status reached CLOSED, the record would have carried a
    doubled winner rather than no winner at all."""
    assert recomputed().realized("LONG") == Decimal("95.2500")


def test_a_short_is_counted_in_its_own_direction():
    """Price rising is a loss on a short. Reusing the long arithmetic would
    repair a losing trade into a winning one."""
    assert recomputed(side="SHORT").realized("SHORT") == Decimal("-95.2500")


def test_fees_are_summed_over_the_counted_fills_only():
    """The excluded fill's charges are not the account's cost, because the
    broker only billed one round trip."""
    assert recomputed().fees == Decimal("6.925")


def test_an_entry_with_no_exit_yet_has_nothing_realised():
    fresh = repair.Recomputed()
    fresh.add(fill(5, minute=0, price="1208.34"), is_entry=True)
    assert fresh.open_quantity == 5
    assert fresh.average_exit is None
    assert fresh.realized("LONG") == Decimal("0")


def test_the_exit_time_is_the_last_counted_fill():
    """The duplicate arrived a minute later. Taking its time would move the
    trade's exit, and with it every clock-based reading of the exit."""
    fresh = recomputed()
    assert fresh.last_exit_at == START + timedelta(minutes=74)
