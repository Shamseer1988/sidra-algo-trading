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

**One tick, ₹0.05, for everything.** NSE's cash-market tick is ₹0.05, except
for low-priced securities where it is finer. Rounding every price to ₹0.05 is
therefore valid everywhere in the segment, because a multiple of ₹0.05 is also
a multiple of ₹0.01 -- the coarser grid is a subset of the finer one. That
property is why this file needs no per-instrument data and cannot be wrong
about a scrip it has never seen. The cost is that a stop on a sub-₹250 share
may sit up to four paise from where the model put it.

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
