"""Prices the exchange will accept, rounded in the direction that is safe.

On 5 October a long in BAJFINANCE filled and its protective stop was rejected:

    You've entered an invalid trigger price. Go back to the order entry screen
    & place an order with the price in multiples of the tick size as mentioned
    by the Exchange.

The trigger sent was ``977.9632``. It came from the signal's stop price, which
is a computed structural level stored as ``Numeric(18, 4)`` and had never been
asked to be a price anyone could actually trade at. The position was left
unprotected -- the fifth time the protection path has failed live, and the
first time for a reason that has nothing to do with broker vocabularies.

**The tick is per instrument, and assuming one was wrong.** This module was
built on the belief that ₹0.05 is valid everywhere in the cash segment, because
a multiple of ₹0.05 is also a multiple of ₹0.01. The premise holds only if no
share trades on a grid *coarser* than ₹0.05, and 461 of them do: NSE's own
instrument master lists 7,973 equities on ₹0.01, 1,349 on ₹0.05, 392 on ₹0.10,
and the rest on ₹0.50, ₹1 or ₹5.

On 7 October a PAYTM entry priced at ₹1,749.95 was rejected — "place an order
with the price in multiples of the tick size" — while an IRCTC entry at ₹449.75
filled the same morning. Both are multiples of ₹0.05. PAYTM trades on ₹0.10.
The trade was simply lost, and on a ₹0.10 share roughly half of all prices this
module produced were unsendable.

So the grid now comes from the broker's instrument master, which was already
being downloaded for every subscribed instrument and having this one field
thrown away. ``TICK`` below remains as the fallback for an instrument whose
grid we do not know, and it is a fallback rather than a constant: being wrong
that way costs a refused order, which is a lost trade and never a lost
position.

**BUY rounds down, SELL rounds up.** One rule, and it is conservative in both
of the places a price is used:

* A limit price never crosses further than asked -- a BUY bids no more, a SELL
  offers no less.
* A stop trigger never widens the loss. The protective stop on a long is a
  SELL below the entry: rounding it up moves it *toward* the entry, so the
  rounding can only reduce what the trade can lose, never increase it. On a
  short the stop is a BUY above the entry and rounding down does the same.

The alternative -- round to nearest -- would be half a tick cheaper on average
and would sometimes widen a stop. On a ₹100 risk budget one tick is ₹0.60 and
the asymmetry is worth more than the average.
"""

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from app.services.broker_adapter import BUY

# NSE cash market. See the module docstring for why one value is safe for every
# instrument in the segment rather than a per-scrip lookup.
# The grid to use when the instrument master has not told us the real one.
# ₹0.05 is valid for the great majority of the segment and fails closed on the
# rest: a price on the wrong grid is refused, not filled badly.
TICK = Decimal("0.05")

# Two paise is below any tick the exchange uses, so a price already on the grid
# is returned untouched rather than nudged by floating-point dust.
_CENTS = Decimal("0.01")


def round_to_tick(price: Decimal | None, side: str, *, tick: Decimal = TICK) -> Decimal | None:
    """Put a price on the exchange's grid, rounding in the safe direction.

    ``None`` and zero pass through unchanged: a market order carries no price
    and a zero is the absence of one, not a price at the bottom of the book.
    """
    if price is None:
        return None
    value = Decimal(str(price))
    if value == 0:
        return value

    rounding = ROUND_FLOOR if side.upper() == BUY else ROUND_CEILING
    on_grid = (value / tick).quantize(Decimal("1"), rounding=rounding) * tick
    return on_grid.quantize(_CENTS)


def is_on_tick(price: Decimal | None, *, tick: Decimal = TICK) -> bool:
    """Whether the exchange would accept this price. Used by the guard tests."""
    if price is None or price == 0:
        return True
    return Decimal(str(price)) % tick == 0
