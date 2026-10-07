"""The worst price a live entry may pay, and how much may be bought there.

On 5 October a signal planned an entry at ₹985.85 behind a stop at ₹977.96 --
₹7.89 a share, twelve shares, ₹95 of a ₹100 budget. It was sent as a MARKET
order and filled at ₹995.40. Against the same structural stop that is ₹17.44 a
share: **₹209 at risk on a ₹100 budget**, decided by the market in the seconds
between the candle closing and the order landing.

Nothing was broken. A market order buys at the market, and the stop belongs to
the strategy's structure rather than to whatever was paid. The budget simply
had no mechanism behind it once the entry stopped being the price the plan
assumed.

This module is that mechanism, and it rests on one observation: **a trade taken
at a materially worse price is a different trade.** The plan was 1:2 from
₹985.85. From ₹995.40 the same stop and the same target are roughly 1:0.4. It
is not the trade that was tested, and taking it is a decision nobody made.

So the entry becomes a limit order at the worst price still worth paying, and
the quantity is sized from *that* price rather than from the hoped-for one.
Both halves are needed. The limit alone bounds the price but not the loss,
because the loss is price times quantity. Sizing alone bounds nothing, because
a market order can fill anywhere. Together they make the budget a ceiling:
whatever happens between the signal and the fill, a trade that fills at all
risks no more than it was given.

**What this costs.** Orders that do not fill. A breakout that runs the moment
it triggers will leave the limit behind and the trade will not be taken -- which
is the point, and is also a real cost on the days the run continues without
you. It will also buy fewer shares than the signal asked for, because the
budget buys less at a worse price.

**What it is not.** Not a guarantee. A gap through the stop overnight, a stop
that cannot be placed, a broker that rejects the exit -- each of those still
exceeds the budget, and no order type prevents them. This bounds the entry
price, which is one of the several ways a trade can lose more than planned, and
the only one that had no defence at all.
"""

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from app.services.broker_adapter import BUY
from app.services.price_ticks import round_to_tick

# Percent of the signal's entry price. Small on purpose: the number is not a
# slippage *tolerance* so much as a statement about when the trade stops being
# the one that was planned.
DEFAULT_CAP_PERCENT = Decimal("0.25")

# The same question asked the way a trader asks it: how much of the trade's risk
# may the entry give away before it is a worse trade? A percent of price cannot
# answer that, because the same percent means different things on different
# stops.
#
# On 7 October an IRCTC short was planned at ₹450.85 behind a ₹454.46 stop --
# ₹3.61 a share, 27 shares, ₹97 of a ₹100 budget. A 0.25% cap moved the limit
# ₹1.13, which is **31% of the whole stop distance**, so the order was sized
# against ₹4.71 a share and only 21 shares were sent. It filled at ₹450.94 and
# risked ₹73.71 -- a quarter of the budget left unused, every trade, because the
# cap was measured against the price instead of against the risk.
#
# A tenth of R moves the limit ₹0.36 on that trade and buys 25. On a wide-stop
# instrument it allows more rupees of drift, and on a tight-stop one fewer,
# which is what "the same tolerance" actually means.
DEFAULT_CAP_R = Decimal("0.10")


@dataclass(frozen=True)
class EntryPlan:
    """How a live entry should be priced and sized, or why it should not be sent."""

    limit_price: Decimal
    quantity: int
    worst_case_risk: Decimal
    # The quantity the signal asked for, kept so the difference can be shown.
    planned_quantity: int
    refusal: str | None = None

    @property
    def reduced(self) -> bool:
        return self.refusal is None and self.quantity < self.planned_quantity


def plan_entry(
    *,
    side: str,
    entry_price: Decimal,
    stop_price: Decimal,
    quantity: int,
    risk_budget: Decimal,
    cap_percent: Decimal = DEFAULT_CAP_PERCENT,
    cap_r: Decimal = DEFAULT_CAP_R,
) -> EntryPlan:
    """Price and size an entry so that filling at the worst allowed price still
    risks no more than ``risk_budget``.

    ``side`` is the canonical BUY or SELL of the *entry*, not of the stop.

    Two ceilings, and the tighter one wins. ``cap_r`` is a fraction of the
    trade's own stop distance and is normally the binding one: it keeps the
    give-away proportional to what the trade is risking, so the same setting
    behaves the same way on a ₹3 stop and a ₹30 one. ``cap_percent`` is a
    fraction of the price and stays as a backstop for the case the first cannot
    see -- a stop so wide that a tenth of it is a large absolute move.

    Either cap at zero means the limit sits exactly at the signal's entry price:
    valid, and the strictest setting available, not a disabled one.
    """
    entry = Decimal(str(entry_price))
    stop = Decimal(str(stop_price))
    budget = Decimal(str(risk_budget))
    planned = int(quantity)

    if entry <= 0 or planned <= 0:
        return EntryPlan(entry, 0, Decimal("0"), planned, "The signal carries no price or no quantity.")

    buying = side.upper() == BUY
    # The cap moves against us: a buy may pay more, a sell may receive less.
    # The tighter of the two ceilings decides, because each exists to catch what
    # the other cannot: one bounds the fraction of risk given away, the other
    # bounds the absolute move.
    planned_room = abs(stop - entry)
    by_price = entry * Decimal(str(cap_percent)) / Decimal("100")
    by_risk = planned_room * Decimal(str(cap_r))
    drift = min(by_price, by_risk)
    cap = entry + drift if buying else entry - drift
    # Rounded by the same rule the order itself uses, so the price planned here
    # and the price sent are the same number rather than two that nearly agree.
    limit = round_to_tick(cap, side) or entry

    # The stop has to stay on the far side of the limit, or the "risk" is a
    # negative number and the sizing below would hand back an enormous quantity.
    room = (limit - stop) if buying else (stop - limit)
    if room <= 0:
        return EntryPlan(
            limit,
            0,
            Decimal("0"),
            planned,
            f"A {cap_percent}% cap puts the entry at ₹{limit}, which is already past the stop at ₹{stop}. "
            "The trade has no room left; nothing was sent.",
        )

    if budget <= 0:
        return EntryPlan(limit, 0, Decimal("0"), planned, "The signal carries no risk budget.")

    # Floor, never round: a share that takes the trade one rupee over budget is
    # a share that should not be bought.
    affordable = int((budget / room).to_integral_value(rounding=ROUND_FLOOR))
    # Never more than the plan asked for. A worse price cannot justify a bigger
    # position, however much budget the arithmetic leaves lying around.
    final = min(planned, affordable)

    if final <= 0:
        return EntryPlan(
            limit,
            0,
            Decimal("0"),
            planned,
            f"At ₹{limit} the stop at ₹{stop} is ₹{room} a share, and a ₹{budget} budget "
            "does not cover one share. Nothing was sent.",
        )

    return EntryPlan(limit, final, (room * final).quantize(Decimal("0.01")), planned)
